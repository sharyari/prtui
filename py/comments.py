"""Comment panel building for prtui."""

from textual.widgets import Static, Collapsible, Markdown
from rich.text import Text
from datetime import datetime, timezone
import store


def _fmt_time(iso):
    """Format an ISO timestamp to a relative date with clock time."""
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
    clock = dt.strftime("%H:%M")
    days = (datetime.now(timezone.utc) - dt).days
    if days == 0:
        return f"today {clock}"
    if days == 1:
        return f"yesterday {clock}"
    if days < 7:
        return f"{days} days ago {clock}"
    return dt.strftime("%Y-%m-%d %H:%M")


def render_diff(diff_hunk):
    """Render a diff hunk with diff colors."""
    result = Text()
    for i, line in enumerate(diff_hunk.split("\n")):
        if i > 0:
            result.append("\n")
        if line.startswith("@@"):
            result.append(line, style="bold cyan")
        elif line.startswith("+"):
            result.append(f"+ {line[1:]}", style="green")
        elif line.startswith("-"):
            result.append(f"- {line[1:]}", style="red")
        else:
            result.append(f"  {line[1:] if line.startswith(' ') else line}")
    return result


_REVIEW_CLASSES = {
    "approval": "review-approval",
    "changes_requested": "review-changes-requested",
    "dismissed": "review-dismissed",
}


def _is_new(thread, read_at):
    """Return True if the thread has comments newer than read_at."""
    if not read_at:
        return True
    return any(c["created_at"] > read_at for c in thread)


def _build_review(thread, _collapsed, new):
    """Build plain Markdown widgets for review entries."""
    cls = _REVIEW_CLASSES.get(thread[0]["type"], "review-dismissed")
    widgets = []
    for c in thread:
        ts = _fmt_time(c["created_at"])
        tag = "🔵 " if new else ""
        md = Markdown(f"{tag}**{c['user']}** ({ts}): {c['comment']}", classes=cls)
        widgets.append(md)
    return widgets


def _build_comment(thread, collapsed, new):
    """Build a Collapsible widget for a regular comment thread."""
    root = thread[0]
    ts = _fmt_time(root["created_at"])
    tag = "🔵 " if new else ""
    title = f"{tag}{root['user']} ({ts})"
    if root["path"]:
        title += f"  {root['path']}"
    children = []
    if root["diff_hunk"]:
        children.append(Static(render_diff(root["diff_hunk"])))
    for j, c in enumerate(thread):
        if j > 0:
            children.append(Markdown("---"))
        ts = _fmt_time(c["created_at"])
        md = Markdown(f"**{c['user']}** ({ts})\n\n{c['comment']}", classes="comment-body")
        children.append(md)
    col = Collapsible(*children, collapsed=collapsed, title=title, classes="comment-thread")
    return [col]


def _build_commit(thread, _collapsed, new):
    """Build plain Markdown widgets for commit entries."""
    widgets = []
    for c in thread:
        ts = _fmt_time(c["created_at"])
        tag = "🔵 " if new else ""
        md = Markdown(f"{tag}**{c['user']}** ({ts}): {c['comment']}", classes="commit")
        widgets.append(md)
    return widgets


_BUILDERS = {
    "approval": _build_review,
    "changes_requested": _build_review,
    "dismissed": _build_review,
    "commit": _build_commit,
    "comment": _build_comment,
}


def _build_thread(thread, collapsed, new):
    """Build widgets from a comment thread, dispatching by type."""
    builder = _BUILDERS.get(thread[0]["type"], _build_comment)
    return builder(thread, collapsed, new)


def populate_panel(panel, repo, number, read_at=None):
    """Build comment thread widgets into the panel."""
    panel.remove_children()
    for thread in store.get_comments(repo, number):
        new = _is_new(thread, read_at)
        collapsed = not new
        for widget in _build_thread(thread, collapsed, new):
            panel.mount(widget)
