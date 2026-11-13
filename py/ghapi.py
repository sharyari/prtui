"""GitHub API client for fetching pull requests, reviews, and comments."""

import requests
import config
import prdb

_cfg = config.read_config()

API = "https://api.github.com"
HEADERS = {
    "Authorization": f"Bearer {_cfg['token']}",
    "Accept": "application/vnd.github+json",
}

REPOS = _cfg["repos"]
USER = _cfg["username"]
TEAM = _cfg.get("team", "")
# team-review-requested needs the org; a bare slug silently matches nothing.
if TEAM and (TEAM.count("/") != 1 or "" in TEAM.split("/")):
    raise SystemExit(f"config: team must be <org>/<team-slug>, got '{TEAM}'")
_CI_URL_PATTERN = _cfg.get("ci-url-pattern", "")
_CUSTOM_QUERY = _cfg.get("custom-query", "")


def _graphql(query, variables=None):
    resp = requests.post(f"{API}/graphql", headers=HEADERS,
                         json={"query": query, "variables": variables or {}})
    resp.raise_for_status()
    data = resp.json()
    if data.get("errors"):
        raise RuntimeError(f"GraphQL: {data['errors']}")
    return data["data"]


# REST shows these bots under a different name than GraphQL.
_BOT_NAMES = {"copilot-pull-request-reviewer": "Copilot",
              "copilot-swe-agent": "Copilot"}


def _login(node):
    """Return the author's login as the REST API shows it."""
    author = node.get("author") or {"login": "ghost"}
    if author.get("__typename") == "Bot":
        return _BOT_NAMES.get(author["login"], f"{author['login']}[bot]")
    return author["login"]


def _stack_label(entry):
    """Format a PR's stack number, position, and size for display."""
    if not entry:
        return ""
    stack = entry["stack"]
    return f"#{stack['number']} ({entry['position']}/{stack['size']})"


_STACK = "stackEntry { position stack { number size } }"

_SEARCH = """
query($q: String!, $after: String) {
  search(query: $q, type: ISSUE, first: 100, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes { ... on PullRequest {
      number title url updatedAt isDraft
      repository { nameWithOwner }
      author { __typename login }
      %s
    } }
  }
}
""" % _STACK


def _search_prs(query, pr_type):
    """Run a GitHub search and return matching PRs."""
    prs, after = [], None
    while True:
        page = _graphql(_SEARCH, {"q": query, "after": after})["search"]
        prs += [{
            "number": item["number"],
            "repo": item["repository"]["nameWithOwner"],
            "author": _login(item),
            "title": item["title"],
            "url": item["url"],
            "updated_at": item["updatedAt"],
            "type": pr_type,
            "draft": item["isDraft"],
            "stack": _stack_label(item["stackEntry"]),
        } for item in page["nodes"]]
        if not page["pageInfo"]["hasNextPage"]:
            return prs
        after = page["pageInfo"]["endCursor"]


def _repo_query():
    """Build the repo: part of a search query."""
    return " ".join(f"repo:{r}" for r in REPOS)


def sections():
    """Return (table id, PR type, title, search queries) for each PR table.

    Listed in priority order: a PR matched by several sections belongs to
    the first one.
    """
    base = f"{_repo_query()} type:pr state:open"
    result = [
        ("prs", "mine", "My PRs", [f"{base} author:{USER}"]),
        ("reviewer", "reviewer", "Reviewing", [
            f"{base} reviewed-by:{USER} -author:{USER}",
            # user-review-requested only accepts @me, i.e. the token's user.
            f"{base} user-review-requested:@me -author:{USER}",
        ]),
        ("requested", "requested", "Team Requested",
         [f"{base} team-review-requested:{TEAM} -author:{USER}"]
         if TEAM else []),
    ]
    if _CUSTOM_QUERY:
        result.append(("custom", "custom",
                       _cfg.get("custom-query-label", "Custom"),
                       [_CUSTOM_QUERY]))
    return result


def _fetch_all_prs():
    """Run all section queries in parallel; one PR list per query."""
    from concurrent.futures import ThreadPoolExecutor
    queries = [(query, pr_type)
               for _, pr_type, _, section_queries in sections()
               for query in section_queries]
    with ThreadPoolExecutor(max_workers=len(queries)) as pool:
        return list(pool.map(lambda q: _search_prs(*q), queries))


# Map GitHub review states to comment types for display.
_REVIEW_TYPE = {
    "APPROVED": "approval",
    "CHANGES_REQUESTED": "changes_requested",
    "DISMISSED": "dismissed",
}


def _list(name, fields, after=None):
    """GraphQL selection for one page of a list field."""
    after = f', after: "{after}"' if after else ""
    return (f"{name}(first: 100{after}) {{ pageInfo {{ hasNextPage endCursor }}"
            f" nodes {{ {fields} }} }}")


_THREAD_COMMENT = ("databaseId author { __typename login } body createdAt"
                   " updatedAt path diffHunk state replyTo { databaseId }")
# The PR's lists, each fetched 100 at a time.
_LISTS = {
    "comments": "databaseId author { __typename login } body createdAt"
                " updatedAt",
    "reviews": "databaseId author { __typename login } state body"
               " submittedAt",
    "reviewThreads": "id " + _list("comments", _THREAD_COMMENT),
    "commits": "commit { oid message committedDate"
               " author { name user { login } } }",
}

_PR_FIELDS = """
fragment pr on PullRequest {
  isDraft mergeable mergeStateStatus headRefName baseRefName headRefOid
  %s
  %s
  headCommit: commits(last: 1) {
    nodes { commit { status { contexts { state targetUrl } } } }
  }
}
""" % (_STACK, "\n  ".join(_list(n, f) for n, f in _LISTS.items()))


def _fetch_remaining_pages(pr, d):
    """Append the pages beyond the first 100 items of each list to d."""
    owner, name = pr["repo"].split("/")
    for conn, fields in _LISTS.items():
        page = d[conn]
        while page["pageInfo"]["hasNextPage"]:
            sel = _list(conn, fields, page["pageInfo"]["endCursor"])
            page = _graphql(
                f'query {{ repository(owner: "{owner}", name: "{name}")'
                f' {{ pullRequest(number: {pr["number"]}) {{ {sel} }} }} }}'
            )["repository"]["pullRequest"][conn]
            d[conn]["nodes"] += page["nodes"]
    for thread in d["reviewThreads"]["nodes"]:
        page = thread["comments"]
        while page["pageInfo"]["hasNextPage"]:
            sel = _list("comments", _THREAD_COMMENT,
                        page["pageInfo"]["endCursor"])
            page = _graphql(
                f'query {{ node(id: "{thread["id"]}")'
                f' {{ ... on PullRequestReviewThread {{ {sel} }} }} }}'
            )["node"]["comments"]
            thread["comments"]["nodes"] += page["nodes"]


def _pr_from_graphql(pr, d):
    """Fill pr from a GraphQL response; returns (pr, comments)."""
    number, repo = pr["number"], pr["repo"]

    def comment(node, **extra):
        return {"pr_number": number, "pr_repo": repo, "user": _login(node),
                "path": "", "diff_hunk": "", "in_reply_to_id": None,
                **extra}

    comments = [comment(c, id=c["databaseId"], body=c["body"],
                        created_at=c["createdAt"], updated_at=c["updatedAt"])
                for c in d["comments"]["nodes"]]
    for thread in d["reviewThreads"]["nodes"]:
        for c in thread["comments"]["nodes"]:
            if c["state"] == "PENDING":
                continue
            comments.append(comment(
                c, id=c["databaseId"], body=c["body"],
                created_at=c["createdAt"], updated_at=c["updatedAt"],
                path=c["path"], diff_hunk=c["diffHunk"],
                in_reply_to_id=(c["replyTo"] or {}).get("databaseId")))

    latest_state = {}
    for r in d["reviews"]["nodes"]:
        state = r["state"]
        if state in ("PENDING", "COMMENTED"):
            continue
        latest_state[_login(r)] = state
        body = f"**[{state}]** {r['body']}" if r["body"] else f"**[{state}]**"
        comments.append(comment(
            r, id=r["databaseId"], body=body, created_at=r["submittedAt"],
            updated_at=r["submittedAt"],
            type=_REVIEW_TYPE.get(state, "comment")))
    approvers = [u for u, s in latest_state.items() if s == "APPROVED"]

    for node in d["commits"]["nodes"]:
        c = node["commit"]
        sha = c["oid"]
        author = (c["author"]["user"] or {}).get("login", c["author"]["name"])
        comments.append(comment(
            {"author": {"login": author}}, id=int(sha[:12], 16),
            body=f"`{sha[:8]}` {c['message'].split(chr(10), 1)[0]}",
            created_at=c["committedDate"], updated_at=c["committedDate"],
            type="commit"))

    mergeable = {"MERGEABLE": d["mergeStateStatus"] != "BLOCKED",
                 "CONFLICTING": False}.get(d["mergeable"])

    # Prefer a pending CI run (active) over a completed one.
    ci_url = None
    if _CI_URL_PATTERN:
        import re
        status = d["headCommit"]["nodes"][0]["commit"]["status"]
        urls = {}
        for s in (status or {}).get("contexts", []):
            match = re.search(_CI_URL_PATTERN, s["targetUrl"] or "")
            if match:
                urls.setdefault(s["state"] == "PENDING", match.group(0))
        ci_url = urls.get(True) or urls.get(False)

    pr.update(approvals=",".join(approvers), mergeable=mergeable,
              ci_url=ci_url, head_sha=d["headRefOid"],
              ci_sha=d["headRefOid"] if ci_url else None,
              draft=d["isDraft"], head_ref=d["headRefName"],
              base_ref=d["baseRefName"],
              stack=_stack_label(d["stackEntry"]))
    return pr, comments


def _fetch_details(prs):
    """Fetch comments, reviews, mergeability, and CI URL for prs.

    Returns [(pr, comments)], fetching 5 PRs per GraphQL query.
    """
    from concurrent.futures import ThreadPoolExecutor

    def fetch_batch(batch):
        parts = []
        for n, pr in enumerate(batch):
            owner, name = pr["repo"].split("/")
            parts.append(f'p{n}: repository(owner: "{owner}", name: "{name}")'
                         f' {{ pullRequest(number: {pr["number"]}) {{ ...pr }} }}')
        data = _graphql("query {\n" + "\n".join(parts) + "\n}" + _PR_FIELDS)
        results = []
        for n, pr in enumerate(batch):
            d = data[f"p{n}"]["pullRequest"]
            _fetch_remaining_pages(pr, d)
            results.append(_pr_from_graphql(pr, d))
        return results

    batches = [prs[i:i + 5] for i in range(0, len(prs), 5)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        return [r for results in pool.map(fetch_batch, batches)
                for r in results]


def poll_for_updates(on_progress=None):
    """Check for new/changed PRs and update the database incrementally.

    Runs the search queries, compares updated_at against the DB,
    re-fetches details only for changed PRs, and removes stale ones.
    Returns True if anything changed.
    """
    def progress(msg):
        if on_progress:
            on_progress(msg)

    progress("Polling PRs...")
    results = _fetch_all_prs()

    with prdb.connection() as cursor:
        prdb.create_pr_table(cursor)
        prdb.create_comments_table(cursor)
        old = prdb.pr_get_updated_at(cursor)
        cursor.execute("SELECT repo, number, stack FROM PRS")
        old_stacks = {(row["repo"], row["number"]): row["stack"]
                      for row in cursor.fetchall()}

    # A PR is stored with a single type, so the first section listing it wins.
    current_keys = set()
    prs = []
    changed = []
    for pr in (pr for result in results for pr in result):
        key = (pr["repo"], pr["number"])
        if key in current_keys:
            continue
        current_keys.add(key)
        prs.append(pr)
        old_ts = old.get(key)
        if old_ts is None or pr["updated_at"] > old_ts:
            changed.append(pr)

    stale = set(old.keys()) - current_keys

    # Stack sizes/positions can change without a PR's updated_at changing.
    changed_keys = {(pr["repo"], pr["number"]) for pr in changed}
    stack_updates = {}
    for pr in prs:
        key = (pr["repo"], pr["number"])
        if key not in changed_keys and old_stacks.get(key, "") != pr["stack"]:
            stack_updates[key] = pr["stack"]

    if not changed and not stale and not stack_updates:
        progress("No changes")
        return False

    # Fetch details only for changed PRs
    comments = []
    progress(f"Updating {len(changed)} PRs...")
    for pr, pr_comments in _fetch_details(changed):
        comments.extend(pr_comments)

    with prdb.connection() as cursor:
        for pr in changed:
            prdb.pr_insert(cursor, pr)
        for comment in comments:
            prdb.comment_insert(cursor, comment)
        for (repo, number), label in stack_updates.items():
            cursor.execute("UPDATE PRS SET stack=? WHERE repo=? AND number=?",
                           (label, repo, number))
        for repo, number in stale:
            prdb.pr_delete(cursor, repo, number)

    updated_count = len(changed) + len(stack_updates)
    progress(f"Updated {updated_count}, removed {len(stale)}")
    return True


if __name__ == "__main__":
    poll_for_updates(on_progress=print)


def refresh_pr(repo, number):
    """Force-refresh a single PR's details and comments in the DB."""
    with prdb.connection() as cursor:
        prdb.create_pr_table(cursor)
        prdb.create_comments_table(cursor)
        cursor.execute(
            "SELECT number, repo, type, author, title, updated_at FROM PRS"
            " WHERE repo=? AND number=?", (repo, number))
        row = cursor.fetchone()
    if not row:
        return False
    [(pr, comments)] = _fetch_details([dict(row)])
    with prdb.connection() as cursor:
        prdb.pr_insert(cursor, pr)
        for comment in comments:
            prdb.comment_insert(cursor, comment)
    return True
