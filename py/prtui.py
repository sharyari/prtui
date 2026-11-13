"""Terminal UI for managing your GitHub pull request inbox."""

from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, DataTable, LoadingIndicator
from textual.widgets import Label, Button
from textual.containers import Vertical, VerticalScroll, Grid
from textual.binding import Binding
from textual.screen import ModalScreen
import os
import sys
import threading
import webbrowser
import subprocess
from pathlib import Path
from datetime import datetime, timezone
import store
import ghapi
import config
from navigation import NavigationMixin
from prtable import PrTable, COLUMNS
import comments
import theme_listener

POLL_INTERVAL = int(config.read_config().get("poll-interval", 120))


class CommentsPanel(VerticalScroll):
    """Scrollable panel for PR comments with its own key bindings."""
    BINDINGS = [
        Binding("q", "close_comments", "Close"),
        Binding("escape", "close_comments", "Close", priority=True),
        Binding("j", "focus_next_table", show=False),
        Binding("k", "focus_prev_table", show=False),
        Binding("h", "noop", show=False),
        Binding("l", "noop", show=False),
        Binding("y", "noop", show=False),
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


class HelpScreen(ModalScreen):
    """Keyboard shortcut reference."""
    BINDINGS = [
        Binding("escape", "dismiss", show=False),
        Binding("q", "dismiss", show=False),
        Binding("question_mark", "dismiss", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Grid(
            Label("[b]prtui — keyboard shortcuts[/b]", id="help-title"),
            Label(
                "[b]Navigation[/b]\n"
                "  j / k         Move cursor down / up\n"
                "  h / l         Move cursor left / right\n"
                "  Tab           Focus next table\n"
                "  Shift+Tab     Focus previous table\n"
                "\n"
                "[b]Actions[/b]\n"
                "  o             Open PR in browser\n"
                "  c             Open comments panel\n"
                "  r             Mark PR as read\n"
                "  b             Open CI build in browser\n"
                "  t             Open linked ticket in browser\n"
                "  y / dbl-click Copy cell under cursor\n"
                "\n"
                "[b]Columns[/b]\n"
                "  [red]●[/red] / [dim]●[/dim]         Unread / read\n"
                "  Target        Branch the PR merges into\n"
                "  Source        Branch the PR merges from\n"
                "  Stack         Stack number and PR position / total\n"
                "  Rdy           Ready for review (✓ = non-draft)\n"
                "  CI            Jenkins approved\n"
                "  App           Number of human approvals (✓ = you approved)\n"
                "  Mrg           Mergeable (✓ = ready)\n"
                "\n"
                "[b]Other[/b]\n"
                "  i             PR details (branch info)\n"
                "  ?             Show this help\n"
                "  q             Quit",
                id="help-body",
            ),
            Button("Close", variant="primary", id="help-close"),
            id="help-dialog",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss()


class PrInfoScreen(ModalScreen):
    """Shows branch and other details for the selected PR."""
    BINDINGS = [
        Binding("escape", "dismiss", show=False),
        Binding("enter", "dismiss", show=False),
        Binding("i", "dismiss", show=False),
    ]

    def __init__(self, pr: dict):
        super().__init__()
        self._pr = pr

    def compose(self) -> ComposeResult:
        pr = self._pr
        head_ref = pr.get("head_ref") or "(unknown)"
        base_ref = pr.get("base_ref") or "(unknown)"
        draft = "Yes" if pr.get("draft") else "No"
        yield Grid(
            Label(f"#{pr['number']} {pr['title']}", id="pr-info-title"),
            Label(
                f"Repo:   {pr['repo']}\n"
                f"Source: {head_ref}\n"
                f"Target: {base_ref}\n"
                f"Author: {pr['author']}\n"
                f"Draft:  {draft}",
                id="pr-info-body",
            ),
            Button("Close", variant="primary", id="pr-info-close"),
            id="pr-info-dialog",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss()

    def action_dismiss(self) -> None:
        self.dismiss()


class CiWarningScreen(ModalScreen):
    """Warns that the stored CI build is for an older commit."""
    BINDINGS = [
        Binding("escape", "dismiss", show=False),
        Binding("enter", "dismiss", show=False),
    ]

    def __init__(self, head_sha: str, ci_sha: str):
        super().__init__()
        self._head_sha = head_sha
        self._ci_sha = ci_sha

    def compose(self) -> ComposeResult:
        yield Grid(
            Label(
                f"CI build is for an older commit.\n"
                f"Head:  {self._head_sha[:12]}\n"
                f"CI:    {self._ci_sha[:12]}",
                id="ci-warn-question"),
            Button("OK", variant="warning", id="ci-warn-ok"),
            id="ci-warn-dialog",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss()

    def action_dismiss(self) -> None:
        self.dismiss()


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
        Binding("u", "refresh_pr", "Refresh PR"),
        Binding("question_mark", "help", "Help"),
        Binding("tab", "focus_next_table", "Next Table", show=True),
        Binding("shift+tab", "focus_prev_table", "Prev Table", show=True),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
        Binding("h", "cursor_left", show=False),
        Binding("l", "cursor_right", show=False),
        Binding("i", "pr_info", show=False),
        Binding("y", "copy_cell", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield LoadingIndicator()
        yield Vertical(
            *(Vertical(PrTable(id=table_id), id=f"group-{table_id}",
                       classes="table-group")
              for table_id, _, _, _ in ghapi.sections()),
            id="tables",
        )
        yield CommentsPanel(id="comments")
        yield Vertical(Label("", id="update-banner"), id="update-banner-wrap")
        yield Footer()

    def on_mount(self) -> None:
        if os.environ.get("_PRTUI_WINDOW_WORKER") == "1":
            self._window_parent_pid = os.getppid()
            self.set_interval(1, self._exit_if_window_parent_gone)
        self._initializing = True
        self.theme = getattr(self, "_initial_theme", "textual-dark")
        self._initializing = False
        for table_id, _, title, _ in ghapi.sections():
            self.query_one(f"#group-{table_id}").border_title = title
        unknown = (config.read_config()["exclude-columns"]
                   - {c.lower() for c in COLUMNS})
        if unknown:
            self.notify(f"Unknown exclude-columns: {', '.join(sorted(unknown))}",
                        severity="warning")
        self.watch(self.screen, "focused", self._on_screen_focused)
        threading.Thread(target=self._fetch_worker, daemon=True).start()
        self.set_interval(POLL_INTERVAL, self._poll_updates)
        theme_listener.start(
            lambda t: self.call_from_thread(setattr, self, "theme", t)
        )

    def _exit_if_window_parent_gone(self) -> None:
        """Exit a served app if its native-window supervisor has died."""
        parent_pid = os.getppid()
        if parent_pid == 1 or parent_pid != self._window_parent_pid:
            self.exit()

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
            self.prs = self._load_prs()
            self.call_from_thread(self._populate_tables)
            # Immediate poll after initial render
            self._do_poll(preserve_focus=True)
            self._check_for_update()
        except Exception as e:
            self.call_from_thread(self.notify, f"Fetch failed: {e}",
                                  severity="error")

    @staticmethod
    def _load_prs():
        """Load PRs from the DB, keyed by table id."""
        return {table_id: store.get_pull_requests(pr_type)
                for table_id, pr_type, _, _ in ghapi.sections()}

    def _do_poll(self, preserve_focus=False):
        """Run a poll and refresh tables from DB (always reloads to catch changes from other instances)."""
        ghapi.poll_for_updates(
            on_progress=lambda msg: self.call_from_thread(
                self.notify, msg)
        )
        new_prs = self._load_prs()
        if new_prs != self.prs:
            self.prs = new_prs
            self.call_from_thread(self._populate_tables, preserve_focus)

    def _poll_updates(self) -> None:
        """Periodically check for PR changes and refresh tables."""
        def worker():
            try:
                self._do_poll(preserve_focus=True)
            except Exception as e:
                self.call_from_thread(self.notify, f"Poll failed: {e}",
                                      severity="error")
            self._check_for_update()
        threading.Thread(target=worker, daemon=True).start()

    def _check_for_update(self) -> None:
        """Check if a newer version exists on the tracked Git upstream."""
        repo_dir = Path(__file__).resolve().parent.parent
        try:
            local = subprocess.run(
                ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            upstream = subprocess.run(
                ["git", "-C", str(repo_dir), "rev-parse", "--abbrev-ref",
                 "--symbolic-full-name", "@{upstream}"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            if not local or "/" not in upstream:
                return
            remote_name, remote_branch = upstream.split("/", 1)

            remote = subprocess.run(
                ["git", "-C", str(repo_dir), "ls-remote", "--heads",
                 remote_name, f"refs/heads/{remote_branch}"],
                capture_output=True, text=True, timeout=10,
            )
            if remote.returncode != 0 or not remote.stdout.strip():
                return
            remote_sha = remote.stdout.split()[0]
            if remote_sha == local:
                (repo_dir / "auto-update.log").unlink(missing_ok=True)
                return

            if config.read_config().get("auto-update"):
                self._attempt_auto_update(repo_dir, remote_name, remote_branch)
            else:
                self.call_from_thread(self._show_update_banner, "update available")
        except Exception as e:
            self.call_from_thread(self.notify, f"Update check failed: {e}",
                                  severity="warning")

    def _attempt_auto_update(self, repo_dir: Path, remote: str, branch: str) -> None:
        """Try a fast-forward pull; only claim success if HEAD changes."""
        log = repo_dir / "auto-update.log"
        try:
            old_head = subprocess.run(
                ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            result = subprocess.run(
                ["git", "-C", str(repo_dir), "pull", "--ff-only", remote, branch],
                capture_output=True, text=True, timeout=30,
            )
            new_head = subprocess.run(
                ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            if result.returncode == 0 and new_head != old_head:
                log.unlink(missing_ok=True)
                self.call_from_thread(
                    self._show_update_banner,
                    "prtui updated — restart to use the new version")
            elif result.returncode == 0:
                log.unlink(missing_ok=True)
            else:
                log.write_text(result.stdout + result.stderr)
                self.call_from_thread(
                    self._show_update_banner,
                    "update available — auto-update failed")
        except Exception as e:
            log.write_text(str(e))
            self.call_from_thread(
                self._show_update_banner,
                "update available — auto-update failed")

    def _show_update_banner(self, message: str) -> None:
        self.query_one("#update-banner", Label).update(message)
        self.query_one("#update-banner-wrap").display = True

    def _show_loading(self, show: bool) -> None:
        self.query_one(LoadingIndicator).display = show

    def _populate_tables(self, preserve_focus=False, screen_width=None) -> None:
        # Save focus state before clearing
        focused_id = None
        focused_row = 0
        focused_column = 0
        if preserve_focus:
            try:
                table = self._focused_table()
                focused_id = table.id
                focused_row = table.cursor_row
                focused_column = table.cursor_column
            except Exception:
                pass

        screen_width = screen_width or self.size.width
        for table_id, prs in self.prs.items():
            self.query_one(f"#{table_id}", PrTable).populate(prs, screen_width)

        # Restore focus or default to first table (skip if comments panel is open)
        if self.query_one("#comments", CommentsPanel).display:
            pass
        elif focused_id:
            table = self.query_one(f"#{focused_id}", DataTable)
            row = min(focused_row, table.row_count - 1)
            if row >= 0:
                table.move_cursor(row=row, column=focused_column)
            table.focus()
        else:
            table = self.query_one("#prs", DataTable)
            table.focus()
        self.call_after_refresh(self._on_screen_focused, self.screen.focused)

    def on_resize(self, event) -> None:
        if hasattr(self, "prs"):
            # self.size still holds the old size while this event is handled.
            self._populate_tables(preserve_focus=True,
                                  screen_width=event.size.width)

    def on_data_table_cell_highlighted(self, event) -> None:
        # Update subtitle as the cursor moves within a table.
        table = event.data_table
        prs = self.prs.get(table.id or "", [])
        if 0 <= event.coordinate.row < len(prs):
            self.sub_title = prs[event.coordinate.row]["title"]

    def _on_screen_focused(self, focused) -> None:
        # Update subtitle when focus moves to a different table (tab key).
        if isinstance(focused, DataTable) and hasattr(self, "prs"):
            prs = self.prs.get(focused.id or "", [])
            if 0 <= focused.cursor_row < len(prs):
                self.sub_title = prs[focused.cursor_row]["title"]

    def action_cursor_down(self) -> None:
        self._focused_table().action_cursor_down()

    def action_cursor_up(self) -> None:
        self._focused_table().action_cursor_up()

    def action_cursor_left(self) -> None:
        self._focused_table().action_cursor_left()

    def action_cursor_right(self) -> None:
        self._focused_table().action_cursor_right()

    def action_mark_read(self) -> None:
        table = self._focused_table()
        self._mark_row_read(table, table.cursor_row)
        panel = self.query_one("#comments", CommentsPanel)
        if panel.display:
            prs = self.prs.get(table.id or "", [])
            repo, number = table.pr_key(table.cursor_row)
            comments.populate_panel(panel, repo, number, prs[table.cursor_row]["read_at"])

    def _selected_pr_key(self):
        """Return (repo, number) for the currently selected PR row."""
        table = self._focused_table()
        if table.row_count == 0:
            return None
        return table.pr_key(table.cursor_row)

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
        repo, number = table.pr_key(row)
        store.mark_read(repo, number)
        prs[row]["state"] = "read"
        prs[row]["read_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        table.dim_row(row)

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
        if webbrowser.open(store.get_pr_url(repo, number)):
            self.action_mark_read()

    def action_open_ci(self) -> None:
        key = self._selected_pr_key()
        if not key:
            return
        repo, number = key
        url = store.get_ci_url(repo, int(number))
        if not url:
            self.notify("No CI link found", severity="warning")
            return
        table = self._focused_table()
        pr = self.prs.get(table.id or "", [])[table.cursor_row]
        # Warn if the CI ran on an older commit than the current HEAD
        if pr.get("head_sha") and pr.get("ci_sha") and pr["head_sha"] != pr["ci_sha"]:
            self.push_screen(CiWarningScreen(pr["head_sha"], pr["ci_sha"]),
                             callback=lambda _: webbrowser.open(url))
        else:
            webbrowser.open(url)

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

    def action_refresh_pr(self) -> None:
        key = self._selected_pr_key()
        if not key:
            return
        repo, number = key
        self.notify(f"Refreshing #{number}…")
        def worker():
            try:
                ghapi.refresh_pr(repo, int(number))
                self.prs = self._load_prs()
                self.call_from_thread(self._populate_tables, True)
                self.call_from_thread(self.notify, f"#{number} refreshed")
            except Exception as e:
                self.call_from_thread(self.notify, f"Refresh failed: {e}",
                                      severity="error")
        threading.Thread(target=worker, daemon=True).start()

    def _handle_quit(self, confirmed: bool) -> None:
        if confirmed:
            self.exit()

    def action_quit(self):
        self.push_screen(QuitScreen(), callback=self._handle_quit)

    def action_copy_cell(self) -> None:
        table = self._focused_table()
        if table.row_count == 0:
            return
        text = table.get_cell_at(table.cursor_coordinate).plain
        if not text:
            self.notify("Nothing to copy", severity="warning")
            return
        self.copy_to_clipboard(text)
        # copy_to_clipboard relies on OSC 52, which some terminals (e.g. macOS
        # Terminal) and textual-serve's browser terminal ignore. Locally on
        # macOS, pbcopy always works.
        if sys.platform == "darwin" and not os.environ.get("SSH_CONNECTION"):
            try:
                subprocess.run(["pbcopy"], input=text, text=True, timeout=5)
            except (OSError, subprocess.SubprocessError):
                pass
        self.notify(f"Copied: {text}")

    def action_pr_info(self) -> None:
        table = self._focused_table()
        if table.row_count == 0:
            return
        prs = self.prs.get(table.id or "", [])
        pr = prs[table.cursor_row]
        self.push_screen(PrInfoScreen(pr))

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

if __name__ == "__main__":
    if "--window" in sys.argv[1:] or "-w" in sys.argv[1:]:
        import window
        window.run_windowed()
    else:
        app = GhMail()
        app._initial_theme = config.load_theme()
        app.run()
