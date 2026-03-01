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

_api_calls = 0


def _paginate(url, params=None):
    """Paginate a GitHub API endpoint, yielding JSON items."""
    global _api_calls
    params = params or {"per_page": 100}
    while url:
        _api_calls += 1
        resp = requests.get(url, headers=HEADERS, params=params)
        resp.raise_for_status()
        data = resp.json()
        yield from (data["items"] if "items" in data else data)
        url = resp.links.get("next", {}).get("url")
        params = {}


def _search_prs(query, pr_type, repo):
    """Run a GitHub search and return matching PRs."""
    return [
        {
            "number": item["number"],
            "repo": repo,
            "author": item["user"]["login"],
            "title": item["title"],
            "url": item["html_url"],
            "updated_at": item["updated_at"],
            "type": pr_type,
        }
        for item in _paginate(f"{API}/search/issues", {"q": query, "per_page": 100})
    ]


def get_my_prs():
    """Fetch open PRs authored by me."""
    prs = []
    for repo in REPOS:
        prs.extend(_search_prs(
            f"repo:{repo} type:pr state:open author:{USER}", "mine", repo
        ))
    return prs


def _get_requested_reviewers(pr_number, repo):
    """Fetch requested reviewers for a PR (users and teams)."""
    global _api_calls
    _api_calls += 1
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


def get_review_prs():
    """Fetch open PRs where I'm involved as a reviewer.

    PRs where I've already reviewed go to 'reviewer'.
    PRs where I'm requested get classified via the requested_reviewers
    endpoint: user match → 'reviewer', team match → 'requested',
    neither → ignored.
    """
    seen = set()
    slug = _team_slug()
    reviewer_prs = []
    requested_prs = []

    # PRs I've already reviewed — always "reviewer"
    for repo in REPOS:
        for pr in _search_prs(
            f"repo:{repo} type:pr state:open reviewed-by:{USER}",
            "reviewer", repo
        ):
            key = (pr["repo"], pr["number"])
            if key not in seen:
                seen.add(key)
                reviewer_prs.append(pr)

    # PRs where I'm requested — classify via endpoint in parallel
    from concurrent.futures import ThreadPoolExecutor
    candidates = []
    for repo in REPOS:
        for pr in _search_prs(
            f"repo:{repo} type:pr state:open review-requested:{USER}",
            "reviewer", repo
        ):
            key = (pr["repo"], pr["number"])
            if key not in seen:
                seen.add(key)
                candidates.append(pr)

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

    return reviewer_prs, requested_prs


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


def _fetch_pr_details(pr):
    """Fetch comments and reviews for a single PR. Returns (pr, comments)."""
    comments = get_comments(pr["number"], pr["repo"])
    approvers, review_comments = get_reviews(pr["number"], pr["repo"])
    comments.extend(review_comments)
    pr["approvals"] = ",".join(approvers)
    return pr, comments


def fetch_and_store(on_progress=None):
    """Fetch PRs and comments from GitHub and store in the database."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def progress(msg):
        if on_progress:
            on_progress(msg)

    progress("Fetching your PRs...")
    global _api_calls
    _api_calls = 0
    prs = get_my_prs()
    progress("Fetching review PRs...")
    reviewer_prs, requested_prs = get_review_prs()
    prs.extend(reviewer_prs)
    prs.extend(requested_prs)

    comments = []
    done = 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_fetch_pr_details, pr): pr for pr in prs}
        for future in as_completed(futures):
            pr, pr_comments = future.result()
            comments.extend(pr_comments)
            done += 1
            progress(f"Fetching comments ({done}/{len(prs)})...")

    with prdb.connection() as cursor:
        prdb.create_pr_table(cursor)
        prdb.create_comments_table(cursor)
        for pr in prs:
            prdb.pr_insert(cursor, pr)
        for comment in comments:
            prdb.comment_insert(cursor, comment)

    progress(f"Done ({_api_calls} API calls)")

if __name__ == "__main__":
    fetch_and_store()
