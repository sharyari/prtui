"""Data store — bridges the database and the UI layer."""

import prdb
import config

_cfg = config.read_config()
JENKINS_USER = _cfg["jenkins-user"]


def has_data():
    """Return True if the database exists."""
    return prdb.db_exists()


def _pr_state(pr):
    """Determine display state based on read_at vs updated_at."""
    if pr["read_at"] is None:
        return "unread"
    if pr["updated_at"] > pr["read_at"]:
        return "unread"
    return "read"


def get_pull_requests(type):
    """Fetch all PRs from the DB, formatted for presentation."""
    if not prdb.db_exists():
        return []
    with prdb.connection() as cursor:
        return [
            {**pr, "state": _pr_state(pr)} for pr in prdb.pr_get_all(cursor, type)
        ]


def mark_read(repo, number):
    """Mark a PR as read now."""
    with prdb.connection() as cursor:
        prdb.pr_mark_read(cursor, repo, number)


def get_pr_url(repo, number):
    """Return the GitHub URL for a PR."""
    return f"https://github.com/{repo}/pull/{number}"


def get_comments(repo, number):
    """Fetch comments for a PR, grouped into threads."""
    with prdb.connection() as cursor:
        comments = prdb.get_comments(cursor, number, repo)

    if JENKINS_USER:
        jenkins = [c for c in comments if c["user"] == JENKINS_USER]
        comments = [c for c in comments if c["user"] != JENKINS_USER]
        if jenkins:
            comments.append(jenkins[-1])

    # Group into threads
    threads = {}
    order = []
    for c in comments:
        root_id = c["in_reply_to_id"] or c["id"]
        if root_id not in threads:
            threads[root_id] = []
            order.append(root_id)
        threads[root_id].append(c)
    return [threads[root_id] for root_id in order]
