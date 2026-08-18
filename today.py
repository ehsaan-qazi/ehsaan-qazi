import datetime
from dateutil import relativedelta
import requests
import os
from lxml import etree
import time
import hashlib

# Fine-grained personal access token with All Repositories access:
# Account permissions: read:Followers, read:Starring, read:Watching
# Repository permissions: read:Commit statuses, read:Contents, read:Issues, read:Metadata, read:Pull Requests
# Issues and pull requests permissions not needed at the moment, but may be used in the future
HEADERS = {'authorization': 'token '+ os.environ['ACCESS_TOKEN']}
USER_NAME = os.environ['USER_NAME'] # 'Andrew6rant'
QUERY_COUNT = {'user_getter': 0, 'follower_getter': 0, 'graph_repos_stars': 0, 'recursive_loc': 0, 'graph_commits': 0, 'loc_query': 0, 'streak_getter': 0, 'languages_getter': 0, 'recent_commits_getter': 0}


def daily_readme(birthday):
    """
    Returns the length of time since I was born
    e.g. 'XX years, XX months, XX days'
    """
    diff = relativedelta.relativedelta(datetime.datetime.today(), birthday)
    return '{} {}, {} {}, {} {}{}'.format(
        diff.years, 'year' + format_plural(diff.years), 
        diff.months, 'month' + format_plural(diff.months), 
        diff.days, 'day' + format_plural(diff.days),
        ' 🎂' if (diff.months == 0 and diff.days == 0) else '')


def format_plural(unit):
    """
    Returns a properly formatted number
    e.g.
    'day' + format_plural(diff.days) == 5
    >>> '5 days'
    'day' + format_plural(diff.days) == 1
    >>> '1 day'
    """
    return 's' if unit != 1 else ''


def simple_request(func_name, query, variables):
    """
    Returns a request, or raises an Exception if the response does not succeed.
    """
    request = requests.post('https://api.github.com/graphql', json={'query': query, 'variables':variables}, headers=HEADERS)
    if request.status_code == 200:
        return request
    raise Exception(func_name, ' has failed with a', request.status_code, request.text, QUERY_COUNT)


def graph_commits(start_date, end_date):
    """
    Uses GitHub's GraphQL v4 API to return my total commit count
    """
    query_count('graph_commits')
    query = '''
    query($start_date: DateTime!, $end_date: DateTime!, $login: String!) {
        user(login: $login) {
            contributionsCollection(from: $start_date, to: $end_date) {
                contributionCalendar {
                    totalContributions
                }
            }
        }
    }'''
    variables = {'start_date': start_date,'end_date': end_date, 'login': USER_NAME}
    request = simple_request(graph_commits.__name__, query, variables)
    return int(request.json()['data']['user']['contributionsCollection']['contributionCalendar']['totalContributions'])


def graph_repos_stars(count_type, owner_affiliation, cursor=None, add_loc=0, del_loc=0):
    """
    Uses GitHub's GraphQL v4 API to return my total repository, star, or lines of code count.
    """
    query_count('graph_repos_stars')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 100, after: $cursor, ownerAffiliations: $owner_affiliation) {
                totalCount
                edges {
                    node {
                        ... on Repository {
                            nameWithOwner
                            stargazers {
                                totalCount
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    request = simple_request(graph_repos_stars.__name__, query, variables)
    if request.status_code == 200:
        if count_type == 'repos':
            return request.json()['data']['user']['repositories']['totalCount']
        elif count_type == 'stars':
            return stars_counter(request.json()['data']['user']['repositories']['edges'])


def recursive_loc(owner, repo_name, data, cache_comment, addition_total=0, deletion_total=0, my_commits=0, cursor=None):
    """
    Uses GitHub's GraphQL v4 API and cursor pagination to fetch 100 commits from a repository at a time
    """
    query_count('recursive_loc')
    query = '''
    query ($repo_name: String!, $owner: String!, $cursor: String, $author_id: ID!) {
        repository(name: $repo_name, owner: $owner) {
            defaultBranchRef {
                target {
                    ... on Commit {
                        history(first: 100, after: $cursor, author: { id: $author_id }) {
                            totalCount
                            edges {
                                node {
                                    ... on Commit {
                                        committedDate
                                    }
                                    author {
                                        user {
                                            id
                                        }
                                    }
                                    deletions
                                    additions
                                }
                            }
                            pageInfo {
                                endCursor
                                hasNextPage
                            }
                        }
                    }
                }
            }
        }
    }'''
    variables = {'repo_name': repo_name, 'owner': owner, 'cursor': cursor, 'author_id': OWNER_ID['id']}
    request = requests.post('https://api.github.com/graphql', json={'query': query, 'variables':variables}, headers=HEADERS) # I cannot use simple_request(), because I want to save the file before raising Exception
    if request.status_code == 200:
        if request.json()['data']['repository']['defaultBranchRef'] != None: # Only count commits if repo isn't empty
            return loc_counter_one_repo(owner, repo_name, data, cache_comment, request.json()['data']['repository']['defaultBranchRef']['target']['history'], addition_total, deletion_total, my_commits)
        else: return 0
    force_close_file(data, cache_comment) # saves what is currently in the file before this program crashes
    if request.status_code == 403:
        raise Exception('Too many requests in a short amount of time!\nYou\'ve hit the non-documented anti-abuse limit!')
    raise Exception('recursive_loc() has failed with a', request.status_code, request.text, QUERY_COUNT)


def loc_counter_one_repo(owner, repo_name, data, cache_comment, history, addition_total, deletion_total, my_commits):
    """
    Recursively call recursive_loc (since GraphQL can only search 100 commits at a time) 
    only adds the LOC value of commits authored by me
    """
    for node in history['edges']:
        my_commits += 1
        addition_total += node['node']['additions']
        deletion_total += node['node']['deletions']

    if history['edges'] == [] or not history['pageInfo']['hasNextPage']:
        return addition_total, deletion_total, my_commits
    else: return recursive_loc(owner, repo_name, data, cache_comment, addition_total, deletion_total, my_commits, history['pageInfo']['endCursor'])


def loc_query(owner_affiliation, comment_size=0, force_cache=False, cursor=None, edges=[]):
    """
    Uses GitHub's GraphQL v4 API to query all the repositories I have access to (with respect to owner_affiliation)
    Queries 60 repos at a time, because larger queries give a 502 timeout error and smaller queries send too many
    requests and also give a 502 error.
    Returns the total number of lines of code in all repositories
    """
    query_count('loc_query')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 60, after: $cursor, ownerAffiliations: $owner_affiliation) {
            edges {
                node {
                    ... on Repository {
                        nameWithOwner
                        defaultBranchRef {
                            target {
                                ... on Commit {
                                    history {
                                        totalCount
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    request = simple_request(loc_query.__name__, query, variables)
    if request.json()['data']['user']['repositories']['pageInfo']['hasNextPage']:   # If repository data has another page
        edges += request.json()['data']['user']['repositories']['edges']            # Add on to the LoC count
        return loc_query(owner_affiliation, comment_size, force_cache, request.json()['data']['user']['repositories']['pageInfo']['endCursor'], edges)
    else:
        return cache_builder(edges + request.json()['data']['user']['repositories']['edges'], comment_size, force_cache)


def cache_builder(edges, comment_size, force_cache, loc_add=0, loc_del=0):
    """
    Checks each repository in edges to see if it has been updated since the last time it was cached
    If it has, run recursive_loc on that repository to update the LOC count
    """
    cached = True # Assume all repositories are cached
    filename = 'cache/'+hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest()+'.txt' # Create a unique filename for each user
    try:
        with open(filename, 'r') as f:
            data = f.readlines()
    except FileNotFoundError: # If the cache file doesn't exist, create it
        data = []
        if comment_size > 0:
            for _ in range(comment_size): data.append('This line is a comment block. Write whatever you want here.\n')
        with open(filename, 'w') as f:
            f.writelines(data)

    if len(data)-comment_size != len(edges) or force_cache: # If the number of repos has changed, or force_cache is True
        cached = False
        flush_cache(edges, filename, comment_size)
        with open(filename, 'r') as f:
            data = f.readlines()

    cache_comment = data[:comment_size] # save the comment block
    data = data[comment_size:] # remove those lines
    for index in range(len(edges)):
        repo_name_with_owner = edges[index]['node']['nameWithOwner']
        repo_hash, commit_count, *__ = data[index].split()
        if repo_hash == hashlib.sha256(repo_name_with_owner.encode('utf-8')).hexdigest():
            try:
                total_repo_commits = edges[index]['node']['defaultBranchRef']['target']['history']['totalCount']
                if int(commit_count) != total_repo_commits:
                    owner, repo_name = repo_name_with_owner.split('/')
                    print(f"   [{index+1}/{len(edges)}] Scanning {repo_name_with_owner} ({total_repo_commits} commits)...")
                    loc = recursive_loc(owner, repo_name, data, cache_comment)
                    data[index] = repo_hash + ' ' + str(total_repo_commits) + ' ' + str(loc[2]) + ' ' + str(loc[0]) + ' ' + str(loc[1]) + '\n'
            except TypeError: # If the repo is empty
                data[index] = repo_hash + ' 0 0 0 0\n'
    with open(filename, 'w') as f:
        f.writelines(cache_comment)
        f.writelines(data)
    for line in data:
        loc = line.split()
        loc_add += int(loc[3])
        loc_del += int(loc[4])
    return [loc_add, loc_del, loc_add - loc_del, cached]


def flush_cache(edges, filename, comment_size):
    """
    Wipes the cache file
    This is called when the number of repositories changes or when the file is first created
    """
    with open(filename, 'r') as f:
        data = []
        if comment_size > 0:
            data = f.readlines()[:comment_size] # only save the comment
    with open(filename, 'w') as f:
        f.writelines(data)
        for node in edges:
            f.write(hashlib.sha256(node['node']['nameWithOwner'].encode('utf-8')).hexdigest() + ' 0 0 0 0\n')


def add_archive():
    """
    Several repositories I have contributed to have since been deleted.
    This function adds them using their last known data
    """
    with open('cache/repository_archive.txt', 'r') as f:
        data = f.readlines()
    old_data = data
    data = data[7:len(data)-3] # remove the comment block    
    added_loc, deleted_loc, added_commits = 0, 0, 0
    contributed_repos = len(data)
    for line in data:
        repo_hash, total_commits, my_commits, *loc = line.split()
        added_loc += int(loc[0])
        deleted_loc += int(loc[1])
        if (my_commits.isdigit()): added_commits += int(my_commits)
    added_commits += int(old_data[-1].split()[4][:-1])
    return [added_loc, deleted_loc, added_loc - deleted_loc, added_commits, contributed_repos]

def force_close_file(data, cache_comment):
    """
    Forces the file to close, preserving whatever data was written to it
    This is needed because if this function is called, the program would've crashed before the file is properly saved and closed
    """
    filename = 'cache/'+hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest()+'.txt'
    with open(filename, 'w') as f:
        f.writelines(cache_comment)
        f.writelines(data)
    print('There was an error while writing to the cache file. The file,', filename, 'has had the partial data saved and closed.')


def stars_counter(data):
    """
    Count total stars in repositories owned by me
    """
    total_stars = 0
    for node in data: total_stars += node['node']['stargazers']['totalCount']
    return total_stars


def update_languages_svg(root, lang_data):
    """
    Updates #lang_line_0 and #lang_line_1 elements in SVG with official GitHub colors
    """
    line0_elem = root.find(".//*[@id='lang_line_0']")
    line1_elem = root.find(".//*[@id='lang_line_1']")
    
    if line0_elem is not None:
        del line0_elem[:]
        for i, (name, pct, color) in enumerate(lang_data[:2]):
            dot_span = etree.Element("tspan", fill=color)
            dot_span.text = "●"
            line0_elem.append(dot_span)
            
            txt_span = etree.Element("tspan")
            txt_span.text = f" {name} {pct:.1f}%" + ("   " if i < 1 else "")
            line0_elem.append(txt_span)

    if line1_elem is not None:
        del line1_elem[:]
        name_abbrevs = {"JavaScript": "JS", "TypeScript": "TS", "Jupyter Notebook": "Jupyter"}
        for i, (name, pct, color) in enumerate(lang_data[2:5]):
            display_name = name_abbrevs.get(name, name)
            dot_span = etree.Element("tspan", fill=color)
            dot_span.text = "●"
            line1_elem.append(dot_span)
            
            txt_span = etree.Element("tspan")
            txt_span.text = f" {display_name} {pct:.1f}%" + ("   " if i < len(lang_data[2:5]) - 1 else "")
            line1_elem.append(txt_span)


def update_recent_commits_svg(root, commits_data):
    """
    Updates commit_0, commit_1, commit_2 with compact prefix and exact 30-char smart truncation
    """
    for i in range(3):
        if i < len(commits_data):
            c = commits_data[i]
            repo_tag = f"[{c['repo']}]"
            sha_str = c['sha']
            msg = c['message'].strip()
            
            # Standardize repo prefix column to 19 characters
            dots_len = max(2, 17 - len(repo_tag))
            dot_str = " " + ("." * (dots_len - 2)) + " "
            
            # Truncate message cleanly so total line is exactly <= 59 characters
            max_msg_len = 30
            if len(msg) > max_msg_len:
                msg_str = msg[:max_msg_len - 3] + "..."
            else:
                msg_str = msg
            
            find_and_replace(root, f"commit_{i}_repo", repo_tag)
            find_and_replace(root, f"commit_{i}_dots", dot_str)
            find_and_replace(root, f"commit_{i}_sha", sha_str)
            find_and_replace(root, f"commit_{i}_msg", msg_str)
        else:
            find_and_replace(root, f"commit_{i}_repo", "")
            find_and_replace(root, f"commit_{i}_dots", "")
            find_and_replace(root, f"commit_{i}_sha", "")
            find_and_replace(root, f"commit_{i}_msg", "")


def svg_overwrite(filename, age_data, commit_data, star_data, repo_data, contrib_data, follower_data, loc_data, streak_data, lang_data, recent_commits):
    """
    Parse SVG files and update elements with age, commits, stars, repositories, lines written, streak, languages, and recent commits
    """
    tree = etree.parse(filename)
    root = tree.getroot()
    justify_format(root, 'age_data', age_data, 49)
    justify_format(root, 'commit_data', commit_data, 22)
    justify_format(root, 'star_data', star_data, 14)
    justify_format(root, 'repo_data', repo_data, 6)
    justify_format(root, 'contrib_data', contrib_data)
    justify_format(root, 'follower_data', follower_data, 10)
    justify_format(root, 'loc_data', loc_data[2], 9)
    justify_format(root, 'loc_add', loc_data[0])
    justify_format(root, 'loc_del', loc_data[1], 7)
    
    current_streak, longest_streak = streak_data
    justify_format(root, 'streak_current', f"{current_streak} day{format_plural(current_streak)}", 16)
    justify_format(root, 'streak_longest', f"{longest_streak} day{format_plural(longest_streak)}", 9)
    
    update_languages_svg(root, lang_data)
    update_recent_commits_svg(root, recent_commits)
    
    tree.write(filename, encoding='utf-8', xml_declaration=True)


def justify_format(root, element_id, new_text, length=0):
    """
    Updates and formats the text of the element, and modifes the amount of dots in the previous element to justify the new text on the svg
    """
    if isinstance(new_text, int):
        new_text = f"{'{:,}'.format(new_text)}"
    new_text = str(new_text)
    find_and_replace(root, element_id, new_text)
    just_len = max(0, length - len(new_text))
    if just_len <= 2:
        dot_map = {0: '', 1: ' ', 2: '. '}
        dot_string = dot_map[just_len]
    else:
        dot_string = ' ' + ('.' * just_len) + ' '
    find_and_replace(root, f"{element_id}_dots", dot_string)


def find_and_replace(root, element_id, new_text):
    """
    Finds the element in the SVG file and replaces its text with a new value
    """
    element = root.find(f".//*[@id='{element_id}']")
    if element is not None:
        element.text = new_text


def commit_counter(comment_size):
    """
    Counts up my total commits, using the cache file created by cache_builder.
    """
    total_commits = 0
    filename = 'cache/'+hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest()+'.txt' # Use the same filename as cache_builder
    with open(filename, 'r') as f:
        data = f.readlines()
    cache_comment = data[:comment_size] # save the comment block
    data = data[comment_size:] # remove those lines
    for line in data:
        total_commits += int(line.split()[2])
    return total_commits


def user_getter(username):
    """
    Returns the account ID and creation time of the user
    """
    query_count('user_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            id
            createdAt
        }
    }'''
    variables = {'login': username}
    request = simple_request(user_getter.__name__, query, variables)
    return {'id': request.json()['data']['user']['id']}, request.json()['data']['user']['createdAt']

def follower_getter(username):
    """
    Returns the number of followers of the user
    """
    query_count('follower_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            followers {
                totalCount
            }
        }
    }'''
    request = simple_request(follower_getter.__name__, query, {'login': username})
    return int(request.json()['data']['user']['followers']['totalCount'])


def streak_getter(username):
    """
    Calculates current streak and longest streak from GitHub contribution calendar
    """
    query_count('streak_getter')
    query = '''
    query ($login: String!) {
        user(login: $login) {
            contributionsCollection {
                contributionCalendar {
                    weeks {
                        contributionDays {
                            contributionCount
                            date
                        }
                    }
                }
            }
        }
    }'''
    request = simple_request(streak_getter.__name__, query, {'login': username})
    data = request.json()
    days = []
    for week in data['data']['user']['contributionsCollection']['contributionCalendar']['weeks']:
        for day in week['contributionDays']:
            days.append((day['date'], day['contributionCount']))
            
    longest_streak = 0
    temp_streak = 0
    for date_str, count in days:
        if count > 0:
            temp_streak += 1
            longest_streak = max(longest_streak, temp_streak)
        else:
            temp_streak = 0

    current_streak = 0
    reversed_days = list(reversed(days))
    start_idx = 0
    if reversed_days and reversed_days[0][1] == 0:
        start_idx = 1 # grace for today if today has 0 commits so far

    for i in range(start_idx, len(reversed_days)):
        if reversed_days[i][1] > 0:
            current_streak += 1
        else:
            break
            
    return current_streak, longest_streak


def languages_getter(username):
    """
    Queries language distribution across owned repositories and official GitHub language colors
    """
    query_count('languages_getter')
    query = '''
    query ($login: String!) {
        user(login: $login) {
            repositories(first: 60, ownerAffiliations: [OWNER], isFork: false) {
                edges {
                    node {
                        languages(first: 10, orderBy: {field: SIZE, direction: DESC}) {
                            edges {
                                size
                                node {
                                    name
                                    color
                                }
                            }
                        }
                    }
                }
            }
        }
    }'''
    request = simple_request(languages_getter.__name__, query, {'login': username})
    lang_data = request.json()
    lang_sizes = {}
    lang_colors = {}
    for repo_edge in lang_data['data']['user']['repositories']['edges']:
        for lang_edge in repo_edge['node']['languages']['edges']:
            name = lang_edge['node']['name']
            color = lang_edge['node']['color'] or '#858585'
            size = lang_edge['size']
            lang_sizes[name] = lang_sizes.get(name, 0) + size
            lang_colors[name] = color

    total_bytes = sum(lang_sizes.values())
    if total_bytes == 0:
        return []
    sorted_langs = sorted(lang_sizes.items(), key=lambda x: x[1], reverse=True)
    result = []
    for name, size in sorted_langs[:5]:
        pct = (size / total_bytes) * 100
        result.append((name, pct, lang_colors[name]))
    return result


def recent_commits_getter(username, author_id):
    """
    Fetches the 3 most recent commits across repositories
    """
    query_count('recent_commits_getter')
    query = '''
    query ($login: String!, $author_id: ID!) {
        user(login: $login) {
            repositories(first: 15, orderBy: {field: PUSHED_AT, direction: DESC}, ownerAffiliations: [OWNER, COLLABORATOR]) {
                edges {
                    node {
                        name
                        defaultBranchRef {
                            target {
                                ... on Commit {
                                    history(first: 5, author: { id: $author_id }) {
                                        edges {
                                            node {
                                                oid
                                                messageHeadline
                                                committedDate
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }'''
    request = simple_request(recent_commits_getter.__name__, query, {'login': username, 'author_id': author_id['id']})
    comm_data = request.json()
    all_commits = []
    for repo in comm_data['data']['user']['repositories']['edges']:
        repo_name = repo['node']['name']
        if repo['node']['defaultBranchRef']:
            for c in repo['node']['defaultBranchRef']['target']['history']['edges']:
                node = c['node']
                all_commits.append({
                    'repo': repo_name,
                    'sha': node['oid'][:7],
                    'message': node['messageHeadline'],
                    'date': node['committedDate']
                })

    all_commits.sort(key=lambda x: x['date'], reverse=True)
    return all_commits[:3]


def query_count(funct_id):
    """
    Counts how many times the GitHub GraphQL API is called
    """
    global QUERY_COUNT
    QUERY_COUNT[funct_id] += 1


def perf_counter(funct, *args):
    """
    Calculates the time it takes for a function to run
    Returns the function result and the time differential
    """
    start = time.perf_counter()
    funct_return = funct(*args)
    return funct_return, time.perf_counter() - start


def formatter(query_type, difference, funct_return=False, whitespace=0):
    """
    Prints a formatted time differential
    Returns formatted result if whitespace is specified, otherwise returns raw result
    """
    print('{:<23}'.format('   ' + query_type + ':'), sep='', end='')
    print('{:>12}'.format('%.4f' % difference + ' s ')) if difference > 1 else print('{:>12}'.format('%.4f' % (difference * 1000) + ' ms'))
    if whitespace:
        return f"{'{:,}'.format(funct_return): <{whitespace}}"
    return funct_return


if __name__ == '__main__':
    """
    Andrew Grant (Andrew6rant), 2022-2025
    """
    print('Calculation times:')
    # define global variable for owner ID and calculate user's creation date
    # e.g {'id': 'MDQ6VXNlcjU3MzMxMTM0'} and 2019-11-03T21:15:07Z for username 'Andrew6rant'
    user_data, user_time = perf_counter(user_getter, USER_NAME)
    OWNER_ID, acc_date = user_data
    formatter('account data', user_time)
    age_data, age_time = perf_counter(daily_readme, datetime.datetime(2006, 5, 6))
    formatter('age calculation', age_time)
    total_loc, loc_time = perf_counter(loc_query, ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'], 7)
    formatter('LOC (cached)', loc_time) if total_loc[-1] else formatter('LOC (no cache)', loc_time)
    commit_data, commit_time = perf_counter(commit_counter, 7)
    star_data, star_time = perf_counter(graph_repos_stars, 'stars', ['OWNER'])
    repo_data, repo_time = perf_counter(graph_repos_stars, 'repos', ['OWNER'])
    contrib_data, contrib_time = perf_counter(graph_repos_stars, 'repos', ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'])
    follower_data, follower_time = perf_counter(follower_getter, USER_NAME)
    streak_data, streak_time = perf_counter(streak_getter, USER_NAME)
    lang_data, lang_time = perf_counter(languages_getter, USER_NAME)
    recent_commits, commits_time = perf_counter(recent_commits_getter, USER_NAME, OWNER_ID)

    # several repositories that I've contributed to have since been deleted.
    if OWNER_ID == {'id': 'MDQ6VXNlcjU3MzMxMTM0'}: # only calculate for user Andrew6rant
        archived_data = add_archive()
        for index in range(len(total_loc)-1):
            total_loc[index] += archived_data[index]
        contrib_data += archived_data[-1]
        commit_data += int(archived_data[-2])

    for index in range(len(total_loc)-1): total_loc[index] = '{:,}'.format(total_loc[index]) # format added, deleted, and total LOC

    svg_overwrite('dark_mode.svg', age_data, commit_data, star_data, repo_data, contrib_data, follower_data, total_loc[:-1], streak_data, lang_data, recent_commits)
    svg_overwrite('light_mode.svg', age_data, commit_data, star_data, repo_data, contrib_data, follower_data, total_loc[:-1], streak_data, lang_data, recent_commits)

    print(f"\nSuccessfully updated 'dark_mode.svg' and 'light_mode.svg'!")
    print(f"Total function time: {'%.4f' % (user_time + age_time + loc_time + commit_time + star_time + repo_time + contrib_time + streak_time + lang_time + commits_time)} s")
    print('Total GitHub GraphQL API calls:', '{:>3}'.format(sum(QUERY_COUNT.values())))
    for funct_name, count in QUERY_COUNT.items(): print('{:<28}'.format('   ' + funct_name + ':'), '{:>6}'.format(count))