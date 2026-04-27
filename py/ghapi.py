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
_CI_URL_PATTERN = _cfg.get("ci-url-pattern", "")
_CUSTOM_QUERY = _cfg.get("custom-query", "")


def _paginate(url, params=None):
    """Paginate a GitHub API endpoint, yielding JSON items."""
    params = params or {"per_page": 100}
    while url:
        resp = requests.get(url, headers=HEADERS, params=params)
        resp.raise_for_status()
        data = resp.json()
        yield from (data["items"] if "items" in data else data)
        url = resp.links.get("next", {}).get("url")
        params = {}


def _search_prs(query, pr_type):
    """Run a GitHub search and return matching PRs."""
    return [
        {
            "number": item["number"],
            "repo": item["repository_url"].replace(f"{API}/repos/", ""),
            "author": item["user"]["login"],
            "title": item["title"],
            "url": item["html_url"],
            "updated_at": item["updated_at"],
            "type": pr_type,
            "draft": item.get("draft", False),
        }
        for item in _paginate(f"{API}/search/issues",
                              {"q": query,
                               "per_page": 100,
                               "advanced_search": "true"})
    ]


def _repo_query():
    """Build the repo: part of a search query."""
    return " ".join(f"repo:{r}" for r in REPOS)


def _fetch_all_prs():
    """Fetch and classify all PRs in parallel.

    Runs the three search queries concurrently, then classifies
    requested PRs via the requested_reviewers endpoint.
    Returns (mine, reviewer, requested) lists.
    """
    from concurrent.futures import ThreadPoolExecutor
    rq = _repo_query()
    queries = [
        (f"{rq} type:pr state:open author:{USER}", "mine"),
        (f"{rq} type:pr state:open reviewed-by:{USER} -author:{USER}", "reviewer"),
        (f"{rq} type:pr state:open review-requested:{USER} -author:{USER}", "requested"),
    ]

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda q: _search_prs(*q), queries))

    mine_prs = results[0]
    reviewed_prs = results[1]
    requested_raw = results[2]

    # Deduplicate: reviewed-by may overlap with review-requested
    seen = {(pr["repo"], pr["number"]) for pr in reviewed_prs}
    candidates = [pr for pr in requested_raw
                  if (pr["repo"], pr["number"]) not in seen]

    # Classify requested PRs via endpoint
    slug = _team_slug()
    reviewer_prs = list(reviewed_prs)
    requested_prs = []

    def _classify(pr):
        users, teams = _get_requested_reviewers(pr["number"], pr["repo"])
        return pr, users, teams

    with ThreadPoolExecutor(max_workers=4) as pool:
        for pr, users, teams in pool.map(_classify, candidates):
            if USER in users:
                pr["type"] = "reviewer"
                reviewer_prs.append(pr)
            elif slug in teams:
                pr["type"] = "requested"
                requested_prs.append(pr)

    return mine_prs, reviewer_prs, requested_prs


def _get_requested_reviewers(pr_number, repo):
    """Fetch requested reviewers for a PR (users and teams)."""
    url = f"{API}/repos/{repo}/pulls/{pr_number}/requested_reviewers"
    resp = requests.get(url, headers=HEADERS)
    resp.raise_for_status()
    data = resp.json()
    users = {u["login"] for u in data.get("users", [])}
    teams = {t["slug"] for t in data.get("teams", [])}
    return users, teams


def _team_slug():
    """Extract the slug (part after /) from the full team name."""
    return TEAM.split("/", 1)[1] if "/" in TEAM else TEAM


# Map GitHub review states to comment types for display.
_REVIEW_TYPE = {
    "APPROVED": "approval",
    "CHANGES_REQUESTED": "changes_requested",
    "DISMISSED": "dismissed",
}


def get_reviews(pr_number, repo):
    """Fetch reviews for a PR.

    Returns (approvers, review_comments) where approvers is a list of
    usernames whose latest non-COMMENTED state is APPROVED, and
    review_comments is a list of comment dicts for storing in the
    COMMENTS table.
    """
    latest_state = {}
    review_comments = []
    for r in _paginate(f"{API}/repos/{repo}/pulls/{pr_number}/reviews"):
        user = r["user"]["login"]
        state = r["state"]
        if state == "PENDING":
            continue
        if state != "COMMENTED":
            latest_state[user] = state
            body = f"**[{state}]** {r['body']}" if r["body"] else f"**[{state}]**"
            review_comments.append({
                "id": r["id"],
                "pr_number": pr_number,
                "pr_repo": repo,
                "user": user,
                "body": body,
                "created_at": r["submitted_at"],
                "updated_at": r["submitted_at"],
                "path": "",
                "diff_hunk": "",
                "in_reply_to_id": None,
                "type": _REVIEW_TYPE.get(state, "comment"),
            })
    approvers = [u for u, s in latest_state.items() if s == "APPROVED"]
    return approvers, review_comments


def get_comments(pr_number, repo):
    """Fetch all comments on a PR (conversation + inline review)."""
    comments = []

    for c in _paginate(f"{API}/repos/{repo}/issues/{pr_number}/comments"):
        comments.append({
            "id": c["id"],
            "pr_number": pr_number,
            "pr_repo": repo,
            "user": c["user"]["login"],
            "body": c["body"],
            "created_at": c["created_at"],
            "updated_at": c["updated_at"],
            "path": "",
            "diff_hunk": "",
            "in_reply_to_id": None,
        })

    for c in _paginate(f"{API}/repos/{repo}/pulls/{pr_number}/comments"):
        comments.append({
            "id": c["id"],
            "pr_number": pr_number,
            "pr_repo": repo,
            "user": c["user"]["login"],
            "body": c["body"],
            "created_at": c["created_at"],
            "updated_at": c["updated_at"],
            "path": c.get("path", ""),
            "diff_hunk": c.get("diff_hunk", ""),
            "in_reply_to_id": c.get("in_reply_to_id"),
        })

    comments.sort(key=lambda c: c["created_at"])
    return comments


def get_commits(pr_number, repo):
    """Fetch commits on a PR, returned as comment-shaped dicts."""
    commits = []
    for c in _paginate(f"{API}/repos/{repo}/pulls/{pr_number}/commits"):
        sha = c["sha"]
        msg = c["commit"]["message"].split("\n", 1)[0]
        author = (c["author"] or {}).get("login", c["commit"]["author"]["name"])
        commits.append({
            "id": int(sha[:12], 16),
            "pr_number": pr_number,
            "pr_repo": repo,
            "user": author,
            "body": f"`{sha[:8]}` {msg}",
            "created_at": c["commit"]["committer"]["date"],
            "updated_at": c["commit"]["committer"]["date"],
            "path": "",
            "diff_hunk": "",
            "in_reply_to_id": None,
            "type": "commit",
        })
    return commits


def _get_pr_details(pr_number, repo):
    """Fetch mergeable status, CI URL, and SHAs for a PR in one API call sequence.

    Returns (mergeable, ci_url, head_sha, ci_sha) where ci_url/ci_sha are None
    if Jenkins hasn't run on the current HEAD (caller should preserve any
    previously stored values).
    """
    import re
    data = requests.get(f"{API}/repos/{repo}/pulls/{pr_number}", headers=HEADERS)
    data.raise_for_status()
    data = data.json()

    # Mergeability
    mergeable = data.get("mergeable")
    if mergeable is not None:
        if not mergeable:
            mergeable = False
        else:
            mergeable = data.get("mergeable_state") != "blocked"

    # CI URL from commit statuses on the current HEAD
    # Prefer a pending status (active run) over completed ones.
    ci_url = None
    ci_sha = None
    sha = data["head"]["sha"]
    if _CI_URL_PATTERN:
        statuses = requests.get(
            f"{API}/repos/{repo}/commits/{sha}/statuses", headers=HEADERS)
        statuses.raise_for_status()
        pending_url = None
        completed_url = None
        for s in statuses.json():  # newest first
            match = re.search(_CI_URL_PATTERN, s.get("target_url", ""))
            if match:
                if s["state"] == "pending" and pending_url is None:
                    pending_url = match.group(0)
                elif s["state"] != "pending" and completed_url is None:
                    completed_url = match.group(0)
                if pending_url and completed_url:
                    break
        chosen = pending_url or completed_url
        if chosen:
            ci_url = chosen
            ci_sha = sha

    return mergeable, ci_url, sha, ci_sha, data.get("draft", False)


def _fetch_pr_details(pr):
    """Fetch comments, reviews, mergeability, and CI URL for a single PR."""
    comments = get_comments(pr["number"], pr["repo"])
    approvers, review_comments = get_reviews(pr["number"], pr["repo"])
    comments.extend(review_comments)
    comments.extend(get_commits(pr["number"], pr["repo"]))
    pr["approvals"] = ",".join(approvers)
    (pr["mergeable"], pr["ci_url"], pr["head_sha"],
     pr["ci_sha"], pr["draft"]) = _get_pr_details(pr["number"], pr["repo"])
    return pr, comments


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
    mine_prs, reviewer_prs, requested_prs = _fetch_all_prs()
    prs = mine_prs + reviewer_prs + requested_prs

    with prdb.connection() as cursor:
        prdb.create_pr_table(cursor)
        prdb.create_comments_table(cursor)
        old = prdb.pr_get_updated_at(cursor)

    current_keys = set()
    changed = []
    for pr in prs:
        key = (pr["repo"], pr["number"])
        current_keys.add(key)
        old_ts = old.get(key)
        if old_ts is None or pr["updated_at"] > old_ts:
            changed.append(pr)

    if _CUSTOM_QUERY:
        main_keys = set(current_keys)
        for pr in _search_prs(_CUSTOM_QUERY, "custom"):
            key = (pr["repo"], pr["number"])
            current_keys.add(key)
            if key not in main_keys:
                old_ts = old.get(key)
                if old_ts is None or pr["updated_at"] > old_ts:
                    changed.append(pr)

    stale = set(old.keys()) - current_keys

    if not changed and not stale:
        progress("No changes")
        return False

    # Fetch details only for changed PRs
    from concurrent.futures import ThreadPoolExecutor, as_completed
    comments = []
    done = 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_fetch_pr_details, pr): pr for pr in changed}
        for future in as_completed(futures):
            pr, pr_comments = future.result()
            comments.extend(pr_comments)
            done += 1
            progress(f"Updating ({done}/{len(changed)})...")

    with prdb.connection() as cursor:
        for pr in changed:
            prdb.pr_insert(cursor, pr)
        for comment in comments:
            prdb.comment_insert(cursor, comment)
        for repo, number in stale:
            prdb.pr_delete(cursor, repo, number)

    progress(f"Updated {len(changed)}, removed {len(stale)}")
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
    pr, comments = _fetch_pr_details(dict(row))
    with prdb.connection() as cursor:
        prdb.pr_insert(cursor, pr)
        for comment in comments:
            prdb.comment_insert(cursor, comment)
    return True
