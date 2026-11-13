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
                               "advanced_search": "false"})
    ]


def _repo_query():
    """Build the repo: part of a search query."""
    return " ".join(f"repo:{r}" for r in REPOS)


def _stack_label(stack):
    """Format GitHub's stack number, position, and size for display."""
    if not stack:
        return ""
    return f"#{stack['number']} ({stack['position']}/{stack['size']})"


def _get_stack_labels(repo):
    """Fetch current stack membership for all PRs in a repository."""
    labels = {}
    for stack in _paginate(f"{API}/repos/{repo}/stacks"):
        members = stack["pull_requests"]
        for position, pr in enumerate(members, 1):
            labels[pr["number"]] = _stack_label({
                "number": stack["number"], "position": position,
                "size": len(members),
            })
    return labels


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
    """Fetch mergeability, CI details, branches, draft status, and stack info.

    Returns (mergeable, ci_url, head_sha, ci_sha, draft, head_ref, base_ref,
    stack). CI URL/SHA are None if Jenkins hasn't run on the current HEAD.
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

    return (mergeable, ci_url, sha, ci_sha, data.get("draft", False),
            data["head"]["ref"], data["base"]["ref"],
            _stack_label(data.get("stack")))


def _fetch_pr_details(pr):
    """Fetch comments, reviews, mergeability, and CI URL for a single PR."""
    comments = get_comments(pr["number"], pr["repo"])
    approvers, review_comments = get_reviews(pr["number"], pr["repo"])
    comments.extend(review_comments)
    comments.extend(get_commits(pr["number"], pr["repo"]))
    pr["approvals"] = ",".join(approvers)
    (pr["mergeable"], pr["ci_url"], pr["head_sha"],
     pr["ci_sha"], pr["draft"],
     pr["head_ref"], pr["base_ref"],
     pr["stack"]) = _get_pr_details(pr["number"], pr["repo"])
    return pr, comments


def poll_for_updates(on_progress=None):
    """Check for new/changed PRs and update the database incrementally.

    Runs the search queries, compares updated_at against the DB,
    re-fetches details only for changed PRs, and removes stale ones.
    Returns True if anything changed.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

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
    repos = sorted({pr["repo"] for pr in prs})
    with ThreadPoolExecutor(max_workers=4) as pool:
        stacks = dict(zip(repos, pool.map(_get_stack_labels, repos)))
    changed_keys = {(pr["repo"], pr["number"]) for pr in changed}
    stack_updates = {}
    for pr in prs:
        key = (pr["repo"], pr["number"])
        label = stacks[pr["repo"]].get(pr["number"], "")
        if key not in changed_keys and old_stacks.get(key, "") != label:
            stack_updates[key] = label

    if not changed and not stale and not stack_updates:
        progress("No changes")
        return False

    # Fetch details only for changed PRs
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
    pr, comments = _fetch_pr_details(dict(row))
    with prdb.connection() as cursor:
        prdb.pr_insert(cursor, pr)
        for comment in comments:
            prdb.comment_insert(cursor, comment)
    return True
