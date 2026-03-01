"""Comment panel building for prtui."""

from textual.widgets import Static, Collapsible, Markdown
from rich.text import Text
import store


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


_REVIEW_COLORS = {
    "approval": "darkgreen",
    "changes_requested": "darkorange",
    "dismissed": "grey",
}


def _build_review(thread, _collapsed):
    """Build plain Markdown widgets for review entries."""
    color = _REVIEW_COLORS.get(thread[0]["type"], "grey")
    widgets = []
    for c in thread:
        md = Markdown(f"**{c['user']}**: {c['comment']}")
        md.styles.background = color
        widgets.append(md)
    return widgets


def _build_comment(thread, collapsed):
    """Build a Collapsible widget for a regular comment thread."""
    root = thread[0]
    title = root["user"]
    if root["path"]:
        title += f"  ({root['path']})"
    children = []
    if root["diff_hunk"]:
        children.append(Static(render_diff(root["diff_hunk"])))
    for j, c in enumerate(thread):
        if j > 0:
            children.append(Markdown("---"))
        children.append(Markdown(f"**{c['user']}**\n\n{c['comment']}"))
    return [Collapsible(*children, collapsed=collapsed, title=title)]


_BUILDERS = {
    "approval": _build_review,
    "changes_requested": _build_review,
    "dismissed": _build_review,
    "comment": _build_comment,
}


def _build_thread(thread, collapsed):
    """Build widgets from a comment thread, dispatching by type."""
    builder = _BUILDERS.get(thread[0]["type"], _build_comment)
    return builder(thread, collapsed)


def populate_panel(panel, repo, number):
    """Build comment thread widgets into the panel."""
    panel.remove_children()
    for i, thread in enumerate(store.get_comments(repo, number)):
        for widget in _build_thread(thread, collapsed=(i != 0)):
            panel.mount(widget)
