"""Data store — bridges the database and the UI layer."""

import re
import prdb
import config

_cfg = config.read_config()
JENKINS_USER = _cfg["jenkins-user"]
USER = _cfg["username"]
_TICKET_PATTERN = _cfg.get("ticket-pattern", "")
_TICKET_URL = _cfg.get("ticket-url", "")


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
        prdb.create_pr_table(cursor)  # ensures any pending migrations (e.g. new columns) are applied
        prs = []
        for pr in prdb.pr_get_all(cursor, type):
            names = [n for n in (pr["approvals"] or "").split(",") if n]
            jenkins = [n for n in names if n == JENKINS_USER]
            others = [n for n in names if n != JENKINS_USER]
            prs.append({
                **pr,
                "state": _pr_state(pr),
                "approval_count": len(others),
                "jenkins_approved": bool(jenkins),
                "my_approved": USER in others,
                "ci_state": pr.get("ci_state"),
            })
        return prs


def mark_read(repo, number):
    """Mark a PR as read now."""
    with prdb.connection() as cursor:
        prdb.pr_mark_read(cursor, repo, number)


def get_ci_url(repo, number):
    """Return the stored CI URL for a PR, or None."""
    with prdb.connection() as cursor:
        return prdb.pr_get_ci_url(cursor, repo, number)


def get_pr_url(repo, number):
    """Return the GitHub URL for a PR."""
    return f"https://github.com/{repo}/pull/{number}"


def get_ticket_url(title):
    """Extract a ticket ID from the PR title and return the ticket URL."""
    if not _TICKET_PATTERN or not _TICKET_URL:
        return None
    match = re.search(_TICKET_PATTERN, title, re.IGNORECASE)
    if not match:
        return None
    return _TICKET_URL.format(ticket=match.group(0))


def get_comments(repo, number):
    """Fetch comments for a PR, grouped into threads."""
    with prdb.connection() as cursor:
        comments = prdb.get_comments(cursor, number, repo)
        if JENKINS_USER:
            comments = [c for c in comments if c["user"] != JENKINS_USER]

    # Group into threads
    threads = {}
    order = []
    for c in comments:
        root_id = c["in_reply_to_id"] or c["id"]
        if root_id not in threads:
            threads[root_id] = []
            order.append(root_id)
        threads[root_id].append(c)
    # Threads ordered latest-first, but comments within each thread oldest-first
    return [list(reversed(threads[root_id])) for root_id in order]
