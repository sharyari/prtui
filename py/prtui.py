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
import subprocess
from pathlib import Path
from datetime import datetime, timezone
import store
import ghapi
import config
import jenkins
from navigation import NavigationMixin
import comments
import theme_listener

STATE_COL = 0
POLL_INTERVAL = int(config.read_config().get("poll-interval", 120))

STATE_DISPLAY = {
    "unread": "●",
    "read": "●",
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
                "  Tab           Focus next table\n"
                "  Shift+Tab     Focus previous table\n"
                "\n"
                "[b]Actions[/b]\n"
                "  o             Open PR in browser\n"
                "  c             Open comments panel\n"
                "  r             Mark PR as read\n"
                "  b             Open CI build in browser\n"
                "  t             Open linked ticket in browser\n"
                "  J             Start Jenkins build\n"
                "\n"
                "[b]Columns[/b]\n"
                "  [red]●[/red] / [dim]●[/dim]         Unread / read\n"
                "  App           Number of human approvals (✓ = you approved)\n"
                "  CI            Jenkins approved\n"
                "  Mrg           Mergeable (✓ = ready, ✗ = conflicts or blocked)\n"
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
                f"From:   {head_ref}\n"
                f"To:     {base_ref}\n"
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


class JenkinsConfirmScreen(ModalScreen[bool]):
    """Confirm starting a Jenkins build for a PR."""
    BINDINGS = [
        Binding("escape", "cancel", show=False),
        Binding("enter", "confirm", show=False),
        Binding("h", "next", show=False),
        Binding("l", "next", show=False),
        Binding("right", "next", show=False),
        Binding("left", "next", show=False),
    ]

    def __init__(self, pr_number, head_ref, base_ref):
        super().__init__()
        self._pr_number = pr_number
        self._head_ref = head_ref
        self._base_ref = base_ref

    def compose(self) -> ComposeResult:
        job_name = f"jjb_pr_start_linux-64_{self._base_ref}"
        yield Grid(
            Label(
                f"Start Jenkins build?\n\n"
                f"PR:   #{self._pr_number}\n"
                f"From: {self._head_ref}\n"
                f"To:   {self._base_ref}\n"
                f"Job:  {job_name}",
                id="jenkins-question"),
            Button("Start", variant="warning", id="jenkins-start"),
            Button("Cancel", variant="primary", id="jenkins-cancel"),
            id="jenkins-dialog",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "jenkins-start":
            self.dismiss(True)
        else:
            self.dismiss(False)

    def action_cancel(self) -> None:
        self.dismiss(False)

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_next(self):
        panel = self.query_one("#jenkins-dialog", Grid)
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
        Binding("u", "refresh_pr", "Refresh PR"),
        Binding("J", "start_jenkins", "Start Jenkins"),
        Binding("question_mark", "help", "Help"),
        Binding("tab", "focus_next_table", "Next Table", show=True),
        Binding("shift+tab", "focus_prev_table", "Prev Table", show=True),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
        Binding("i", "pr_info", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield LoadingIndicator()
        yield Vertical(
            Vertical(DataTable(id="prs"), id="group-prs", classes="table-group"),
            Vertical(DataTable(id="reviewer"), id="group-reviewer", classes="table-group"),
            Vertical(DataTable(id="requested"), id="group-requested", classes="table-group"),
            Vertical(DataTable(id="custom"), id="group-custom", classes="table-group"),
            id="tables",
        )
        yield CommentsPanel(id="comments")
        yield Vertical(Label("", id="update-banner"), id="update-banner-wrap")
        yield Footer()

    def on_mount(self) -> None:
        self._initializing = True
        self.theme = getattr(self, "_initial_theme", "textual-dark")
        self._initializing = False
        self.query_one("#group-prs").border_title = "My PRs"
        self.query_one("#group-reviewer").border_title = "Reviewing"
        self.query_one("#group-requested").border_title = "Team Requested"
        _cq = config.read_config().get("custom-query", "")
        if _cq:
            _label = config.read_config().get("custom-query-label", "Custom")
            self.query_one("#group-custom").border_title = _label
        else:
            self.query_one("#group-custom").display = False
        self.watch(self.screen, "focused", self._on_screen_focused)
        threading.Thread(target=self._fetch_worker, daemon=True).start()
        self.set_interval(POLL_INTERVAL, self._poll_updates)
        theme_listener.start(
            lambda t: self.call_from_thread(setattr, self, "theme", t)
        )

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
            if config.read_config().get("custom-query"):
                self.prs["custom"] = store.get_pull_requests("custom")
            self.call_from_thread(self._populate_tables)
            # Immediate poll after initial render
            self._do_poll(preserve_focus=True)
            self._check_for_update()
        except Exception as e:
            self.call_from_thread(self.notify, f"Fetch failed: {e}",
                                  severity="error")

    def _do_poll(self, preserve_focus=False):
        """Run a poll and refresh tables from DB (always reloads to catch changes from other instances)."""
        ghapi.poll_for_updates(
            on_progress=lambda msg: self.call_from_thread(
                self.notify, msg)
        )
        new_prs = {
            "prs": store.get_pull_requests("mine"),
            "reviewer": store.get_pull_requests("reviewer"),
            "requested": store.get_pull_requests("requested"),
        }
        if config.read_config().get("custom-query"):
            new_prs["custom"] = store.get_pull_requests("custom")
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
        """Check if a newer version of prtui is available via GitHub API."""
        import requests as _requests
        repo_dir = Path(__file__).resolve().parent.parent
        try:
            local = subprocess.run(
                ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            if not local:
                return
            branch = subprocess.run(
                ["git", "-C", str(repo_dir), "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            if not branch or branch == "HEAD":
                return
            cfg = config.read_config()
            headers = {
                "Authorization": f"Bearer {cfg['token']}",
                "Accept": "application/vnd.github+json",
            }
            resp = _requests.get(
                f"https://api.github.com/repos/sharyari/prtui/commits/{branch}",
                headers=headers, timeout=10,
            )
            if resp.status_code != 200:
                return
            remote_sha = resp.json().get("sha", "")
            if remote_sha and remote_sha != local:
                if cfg.get("auto-update"):
                    self._attempt_auto_update(repo_dir)
                else:
                    self.call_from_thread(self._show_update_banner, "update available")

        except Exception as e:
            self.call_from_thread(self.notify, f"Update check failed: {e}",
                                  severity="warning")

    def _attempt_auto_update(self, repo_dir: Path) -> None:
        """Try git pull; show result in the banner."""
        log = repo_dir / "auto-update.log"
        try:
            result = subprocess.run(
                ["git", "-C", str(repo_dir), "pull", "--ff-only"],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                self.call_from_thread(
                    self._show_update_banner,
                    "prtui updated — restart to use the new version")
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

        # Fixed columns: ●(1) + #(5) + Repo(16) + Author(15) + App(4) + CI(2) + Mrg(3)
        # + column padding (8 cols × 2) + border/padding (4) ≈ 62
        title_width = max(20, self.size.width - 74)

        cfg = config.read_config()
        repo_name_map = cfg.get("repo-name-map")
        for table_id, prs in self.prs.items():
            table = self.query_one(f"#{table_id}", DataTable)
            table.clear(columns=True)
            table.cursor_type = "row"
            table.zebra_stripes = True
            table.add_columns("", "#", "Repo", "Title", "Author", "App", "CI", "Mrg", "Rdy")
            for pr in prs:
                ci = "✓" if pr["jenkins_approved"] else ""
                approvals = str(pr["approval_count"]) if pr["approval_count"] else ""
                if pr.get("my_approved"):
                    approvals = f"✓ {approvals}".strip()
                mrg = {1: "✓", 0: "✗"}.get(pr.get("mergeable"), "")
                draft = "✗" if pr.get("draft") else "✓"
                style = "dim" if pr["state"] == "read" else ""
                state_text = Text(STATE_DISPLAY[pr["state"]],
                                  style="dim" if pr["state"] == "read" else "red")
                cells = [
                    str(pr["number"]),
                    repo_name_map.get(pr["repo"], pr["repo"]),
                    pr["title"][:title_width] + ("…" if len(pr["title"]) > title_width else ""),
                    pr["author"][:15] + ("…" if len(pr["author"]) > 15 else ""),
                    approvals,
                    ci,
                    mrg,
                    draft,
                ]
                table.add_row(
                    state_text,
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
            table = self.query_one("#prs", DataTable)
            table.focus()
        self.call_after_refresh(self._on_screen_focused, self.screen.focused)

    @staticmethod
    def _get_pr_key(table, row):
        """Return (repo, number) from a specific row's key."""
        row_key, _ = table.coordinate_to_cell_key(Coordinate(row, 0))
        return row_key.value.rsplit("#", 1)

    def on_resize(self, event) -> None:
        if hasattr(self, "prs"):
            self._populate_tables(preserve_focus=True)

    def on_data_table_row_highlighted(self, event) -> None:
        # Update subtitle as the cursor moves within a table.
        table = event.data_table
        prs = self.prs.get(table.id or "", [])
        if 0 <= event.cursor_row < len(prs):
            self.sub_title = prs[event.cursor_row]["title"]

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
        table.update_cell_at(Coordinate(row, STATE_COL), Text(STATE_DISPLAY["read"], style="dim"))
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
        if webbrowser.open(store.get_pr_url(repo, number)):
            self.action_mark_read()

    def action_open_ci(self) -> None:
        key = self._selected_pr_key()
        if not key:
            return
        repo, number = key
        table = self._focused_table()
        pr = self.prs.get(table.id or "", [])[table.cursor_row]
        # Try the dashboard API for the freshest build URL
        head_ref = pr.get("head_ref")
        url = None
        if isinstance(head_ref, str) and head_ref:
            url = ghapi.get_dashboard_ci_url(head_ref)
        if not url:
            url = store.get_ci_url(repo, int(number))
        if not url:
            self.notify("No CI link found", severity="warning")
            return
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
                self.prs = {
                    "prs": store.get_pull_requests("mine"),
                    "reviewer": store.get_pull_requests("reviewer"),
                    "requested": store.get_pull_requests("requested"),
                }
                self.call_from_thread(self._populate_tables, True)
                self.call_from_thread(self.notify, f"#{number} refreshed")
            except Exception as e:
                self.call_from_thread(self.notify, f"Refresh failed: {e}",
                                      severity="error")
        threading.Thread(target=worker, daemon=True).start()

    def action_start_jenkins(self) -> None:
        if not jenkins.is_configured():
            self.notify("Jenkins not configured (need ~/.ghmanager_tokens)",
                        severity="warning")
            return
        table = self._focused_table()
        if table.row_count == 0:
            return
        prs = self.prs.get(table.id or "", [])
        pr = prs[table.cursor_row]
        head_ref = pr.get("head_ref")
        base_ref = pr.get("base_ref")
        if not head_ref or not base_ref:
            self.notify("Branch info missing — try refreshing the PR first", severity="warning")
            return
        self.push_screen(
            JenkinsConfirmScreen(pr["number"], head_ref, base_ref),
            callback=lambda confirmed: self._do_start_jenkins(confirmed, pr),
        )

    def _do_start_jenkins(self, confirmed, pr) -> None:
        if not confirmed:
            return
        self.notify(f"Starting Jenkins for #{pr['number']}…")
        def worker():
            success, msg = jenkins.start_test(
                pr["number"], pr["repo"], pr["head_ref"], pr["base_ref"],
            )
            severity = "information" if success else "error"
            self.call_from_thread(self.notify, msg, severity=severity)
            if success:
                # Refresh the PR so the new pending CI status is picked up
                import time
                time.sleep(5)
                try:
                    ghapi.refresh_pr(pr["repo"], int(pr["number"]))
                    self.prs = {
                        "prs": store.get_pull_requests("mine"),
                        "reviewer": store.get_pull_requests("reviewer"),
                        "requested": store.get_pull_requests("requested"),
                    }
                    self.call_from_thread(self._populate_tables, True)
                except Exception:
                    pass
        threading.Thread(target=worker, daemon=True).start()

    def _handle_quit(self, confirmed: bool) -> None:
        if confirmed:
            self.exit()

    def action_quit(self):
        self.push_screen(QuitScreen(), callback=self._handle_quit)

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
    import sys
    if "--window" in sys.argv[1:] or "-w" in sys.argv[1:]:
        import window
        window.run_windowed()
    else:
        app = GhMail()
        app._initial_theme = config.load_theme()
        app.run()
