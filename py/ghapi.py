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

    # PRs where I'm requested — classify via endpoint
    for repo in REPOS:
        for pr in _search_prs(
            f"repo:{repo} type:pr state:open review-requested:{USER}",
            "reviewer", repo
        ):
            key = (pr["repo"], pr["number"])
            if key in seen:
                continue
            seen.add(key)
            users, teams = _get_requested_reviewers(pr["number"], pr["repo"])
            if USER in users:
                pr["type"] = "reviewer"
                reviewer_prs.append(pr)
            elif slug in teams:
                pr["type"] = "requested"
                requested_prs.append(pr)

    return reviewer_prs, requested_prs


def get_approvals(pr_number, repo):
    """Fetch reviews for a PR, returning list of approving usernames."""
    reviews = {}
    for r in _paginate(f"{API}/repos/{repo}/pulls/{pr_number}/reviews"):
        if r["state"] == "COMMENTED":
            continue
        user = r["user"]["login"]
        reviews[user] = {
            "user": user,
            "state": r["state"],
            "submitted_at": r["submitted_at"],
        }
    return [
        user for user, review in reviews.items()
        if review["state"] == "APPROVED"
    ]


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


def fetch_and_store(on_progress=None):
    """Fetch PRs and comments from GitHub and store in the database."""
    def progress(msg):
        if on_progress:
            on_progress(msg)

    progress("Fetching your PRs...")
    prs = get_my_prs()
    progress("Fetching review PRs...")
    reviewer_prs, requested_prs = get_review_prs()
    prs.extend(reviewer_prs)
    prs.extend(requested_prs)

    comments = []
    for i, pr in enumerate(prs):
        progress(f"Fetching comments ({i + 1}/{len(prs)})...")
        comments.extend(get_comments(pr["number"], pr["repo"]))

    with prdb.connection() as cursor:
        prdb.create_pr_table(cursor)
        prdb.create_comments_table(cursor)
        for pr in prs:
            prdb.pr_insert(cursor, pr)
        for comment in comments:
            prdb.comment_insert(cursor, comment)

if __name__ == "__main__":
    fetch_and_store()
