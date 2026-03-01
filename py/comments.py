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


def _build_thread(thread, collapsed):
    """Build a Collapsible widget from a comment thread."""
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
    return Collapsible(*children, collapsed=collapsed, title=title)


def populate_panel(panel, repo, number):
    """Build comment thread widgets into the panel."""
    panel.remove_children()
    for i, thread in enumerate(store.get_comments(repo, number)):
        panel.mount(_build_thread(thread, collapsed=(i != 0)))
