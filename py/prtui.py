"""Terminal UI for managing your GitHub pull request inbox."""

from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, DataTable, LoadingIndicator
from textual.widgets import Label, Button
from textual.containers import Vertical, VerticalScroll, Grid
from textual.binding import Binding
from textual.coordinate import Coordinate
from textual.screen import ModalScreen
from rich.text import Text
import threading
import webbrowser
from datetime import datetime, timezone
import store
import ghapi
import config
from navigation import NavigationMixin
import comments

STATE_COL = 0
POLL_INTERVAL = int(config.read_config().get("poll-interval", 120))

STATE_DISPLAY = {
    "unread": "● new",
    "read": "  read",
}

class CommentsPanel(VerticalScroll):
    """Scrollable panel for PR comments with its own key bindings."""
    BINDINGS = [
        Binding("q", "close_comments", "Close"),
        Binding("escape", "close_comments", "Close", priority=True),
        Binding("j", "focus_next_table", show=False),
        Binding("k", "focus_prev_table", show=False),
        Binding("down", "focus_next_table", show=False, priority=True),
        Binding("up", "focus_prev_table", show=False, priority=True),
        # Shadow app bindings that don't apply here
        Binding("r", "noop", show=False),
        Binding("o", "noop", show=False),
        Binding("b", "noop", show=False),
        Binding("t", "noop", show=False),
        Binding("c", "close_comments", show=False),
    ]

    def action_close_comments(self) -> None:
        self.app.action_close_comments()

    def action_focus_next_table(self) -> None:
        self.app.action_focus_next_table()

    def action_focus_prev_table(self) -> None:
        self.app.action_focus_prev_table()

    def action_noop(self) -> None:
        pass

class QuitScreen(ModalScreen[bool]):
    """Screen with a dialog to quit."""
    BINDINGS = [
        Binding("escape", "dismiss", show= False),
        Binding("h", "next", show=False),
        Binding("l", "next", show=False),
        Binding("right", "next", show=False),
        Binding("left", "next", show=False),
    ]
    def compose(self) -> ComposeResult:
        yield Grid(
            Label("Are you sure you want to quit?", id="question"),
            Button("Quit", variant="error", id="quit"),
            Button("Cancel", variant="primary", id="cancel"),
            id="dialog",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "quit":
            self.dismiss(True)
        else:
            self.dismiss(False)

    def action_dismiss(self) -> None:
        self.dismiss()

    def action_next(self):
        panel = self.query_one("#dialog", Grid)
        buttons = list(panel.query(Button))
        node = self.focused
        if isinstance(node, Button) and node in buttons:
            idx = buttons.index(node)
            target = buttons[(idx + 1) % len(buttons)]
            target.focus()


class GhMail(NavigationMixin, App):
    CSS_PATH = "prtui.tcss"

    TITLE = "prtui"
    SUB_TITLE = "GitHub Pull Request Inbox"

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "mark_read", "Mark Read"),
        Binding("o", "open_pr", "Open PR"),
        Binding("b", "open_ci", "Open CI"),
        Binding("t", "open_ticket", "Open Ticket"),
        Binding("c", "open_comments", "Open Comments"),
        Binding("tab", "focus_next_table", "Next Table", show=True),
        Binding("shift+tab", "focus_prev_table", "Prev Table", show=True),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield LoadingIndicator()
        yield Vertical(
            Vertical(DataTable(id="prs"), id="group-prs", classes="table-group"),
            Vertical(DataTable(id="reviewer"), id="group-reviewer", classes="table-group"),
            Vertical(DataTable(id="requested"), id="group-requested", classes="table-group"),
            id="tables",
        )
        yield CommentsPanel(id="comments")
        yield Footer()

    def on_mount(self) -> None:
        self._initializing = True
        self.theme = getattr(self, "_initial_theme", "textual-dark")
        self._initializing = False
        self.query_one("#group-prs").border_title = "My PRs"
        self.query_one("#group-reviewer").border_title = "Reviewing"
        self.query_one("#group-requested").border_title = "Team Requested"
        threading.Thread(target=self._fetch_worker, daemon=True).start()
        self.set_interval(POLL_INTERVAL, self._poll_updates)

    def watch_theme(self, theme: str) -> None:
        if not getattr(self, "_initializing", False):
            config.save_theme(theme)

    def _fetch_worker(self) -> None:
        """Load DB, populate tables, then poll for updates."""
        try:
            if not store.has_data():
                self.call_from_thread(self._show_loading, True)
                try:
                    ghapi.poll_for_updates(
                        on_progress=lambda msg: self.call_from_thread(
                            self.notify, msg)
                    )
                finally:
                    self.call_from_thread(self._show_loading, False)
            self.prs = {
                "prs": store.get_pull_requests("mine"),
                "reviewer": store.get_pull_requests("reviewer"),
                "requested": store.get_pull_requests("requested"),
            }
            self.call_from_thread(self._populate_tables)
            # Immediate poll after initial render
            self._do_poll(preserve_focus=True)
        except Exception as e:
            self.call_from_thread(self.notify, f"Fetch failed: {e}",
                                  severity="error")

    def _do_poll(self, preserve_focus=False):
        """Run a poll and refresh tables if anything changed."""
        changed = ghapi.poll_for_updates(
            on_progress=lambda msg: self.call_from_thread(
                self.notify, msg)
        )
        if changed:
            self.prs = {
                "prs": store.get_pull_requests("mine"),
                "reviewer": store.get_pull_requests("reviewer"),
                "requested": store.get_pull_requests("requested"),
            }
            self.call_from_thread(self._populate_tables, preserve_focus)

    def _poll_updates(self) -> None:
        """Periodically check for PR changes and refresh tables."""
        def worker():
            try:
                self._do_poll(preserve_focus=True)
            except Exception as e:
                self.call_from_thread(self.notify, f"Poll failed: {e}",
                                      severity="error")
        threading.Thread(target=worker, daemon=True).start()

    def _show_loading(self, show: bool) -> None:
        self.query_one(LoadingIndicator).display = show

    def _populate_tables(self, preserve_focus=False) -> None:
        # Save focus state before clearing
        focused_id = None
        focused_row = 0
        if preserve_focus:
            try:
                table = self._focused_table()
                focused_id = table.id
                focused_row = table.cursor_row
            except Exception:
                pass

        for table_id, prs in self.prs.items():
            table = self.query_one(f"#{table_id}", DataTable)
            table.clear(columns=True)
            table.cursor_type = "row"
            table.zebra_stripes = True
            table.add_columns("State", "#", "Repo", "Title", "Author", "Approvals", "CI")
            for pr in prs:
                ci = "✓" if pr["jenkins_approved"] else ""
                approvals = str(pr["approval_count"]) if pr["approval_count"] else ""
                if pr.get("my_approved"):
                    approvals = f"✓ {approvals}".strip()
                style = "dim" if pr["state"] == "read" else ""
                cells = [
                    STATE_DISPLAY[pr["state"]],
                    str(pr["number"]),
                    pr["repo"],
                    pr["title"],
                    pr["author"],
                    approvals,
                    ci,
                ]
                table.add_row(
                    *(Text(c, style=style) for c in cells),
                    key=f"{pr['repo']}#{pr['number']}",
                )

        # Restore focus or default to first table (skip if comments panel is open)
        if self.query_one("#comments", CommentsPanel).display:
            pass
        elif focused_id:
            table = self.query_one(f"#{focused_id}", DataTable)
            row = min(focused_row, table.row_count - 1)
            if row >= 0:
                table.move_cursor(row=row)
            table.focus()
        else:
            self.query_one("#prs", DataTable).focus()

    @staticmethod
    def _get_pr_key(table, row):
        """Return (repo, number) from a specific row's key."""
        row_key, _ = table.coordinate_to_cell_key(Coordinate(row, 0))
        return row_key.value.rsplit("#", 1)

    def action_cursor_down(self) -> None:
        self._focused_table().action_cursor_down()

    def action_cursor_up(self) -> None:
        self._focused_table().action_cursor_up()

    def action_mark_read(self) -> None:
        table = self._focused_table()
        self._mark_row_read(table, table.cursor_row)
        panel = self.query_one("#comments", CommentsPanel)
        if panel.display:
            prs = self.prs.get(table.id or "", [])
            repo, number = self._get_pr_key(table, table.cursor_row)
            comments.populate_panel(panel, repo, number, prs[table.cursor_row]["read_at"])

    def _selected_pr_key(self):
        """Return (repo, number) for the currently selected PR row."""
        table = self._focused_table()
        if table.row_count == 0:
            return None
        return self._get_pr_key(table, table.cursor_row)

    def _hide_comments(self) -> None:
        panel = self.query_one("#comments", CommentsPanel)
        panel.display = False
        table = self.query_one(f"#{self._comments_source}", DataTable)
        self._mark_row_read(table, self._comments_row)
        table.focus()

    def _mark_row_read(self, table, row) -> None:
        """Mark a specific row's PR as read, updating DB and UI."""
        prs = self.prs.get(table.id or "", [])
        if row >= len(prs) or prs[row]["state"] == "read":
            return
        repo, number = self._get_pr_key(table, row)
        store.mark_read(repo, number)
        prs[row]["state"] = "read"
        prs[row]["read_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        table.update_cell_at(Coordinate(row, STATE_COL), STATE_DISPLAY["read"])
        # Dim the entire row
        for col in range(len(table.columns)):
            val = table.get_cell_at(Coordinate(row, col))
            table.update_cell_at(Coordinate(row, col), Text(str(val), style="dim"))

    def _show_comments(self) -> None:
        key = self._selected_pr_key()
        if not key:
            return
        repo, number = key
        self._comments_source = self._focused_table().id or "prs"
        self._comments_row = self._focused_table().cursor_row
        table = self._focused_table()
        prs = self.prs.get(table.id or "", [])
        read_at = prs[table.cursor_row].get("read_at")
        panel = self.query_one("#comments", CommentsPanel)
        comments.populate_panel(panel, repo, number, read_at)
        title = prs[table.cursor_row].get("title", "")
        panel.border_title = f"#{number} {title}"
        panel.border_subtitle = "ESC to close"
        panel.display = True
        panel.focus()

    def action_open_comments(self) -> None:
        if not self.query_one("#comments", CommentsPanel).display:
            self._show_comments()

    def action_close_comments(self) -> None:
        if self.query_one("#comments", CommentsPanel).display:
            self._hide_comments()

    def action_open_pr(self) -> None:
        key = self._selected_pr_key()
        if not key:
            return
        repo, number = key
        webbrowser.open(store.get_pr_url(repo, number))

    def action_open_ci(self) -> None:
        key = self._selected_pr_key()
        if not key:
            return
        repo, number = key
        url = store.get_ci_url(repo, number)
        if url:
            webbrowser.open(url)
        else:
            self.notify("No CI link found", severity="warning")

    def action_open_ticket(self) -> None:
        table = self._focused_table()
        if table.row_count == 0:
            return
        prs = self.prs.get(table.id or "", [])
        title = prs[table.cursor_row].get("title", "")
        url = store.get_ticket_url(title)
        if url:
            webbrowser.open(url)
        else:
            self.notify("No ticket found in title", severity="warning")

    def _handle_quit(self, confirmed: bool) -> None:
        if confirmed:
            self.exit()

    def action_quit(self):
        self.push_screen(QuitScreen(), callback=self._handle_quit)

if __name__ == "__main__":
    app = GhMail()
    app._initial_theme = config.load_theme()
    app.run()
