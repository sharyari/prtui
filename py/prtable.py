"""PR table widget: columns, cell formatting and column widths."""

from rich.cells import cell_len
from rich.text import Text
from textual.coordinate import Coordinate
from textual.widgets import DataTable

import config

STATE_COL = 0

STATE_DISPLAY = {
    "unread": "●",
    "read": "●",
}
COLUMNS = ["#", "Rdy", "CI", "App", "Mrg", "Title", "Source", "Target",
           "Repo", "Author", "Stack"]
CHECK_COLUMNS = {"Rdy", "CI", "App", "Mrg"}
# Columns that shrink to fit the screen, but never below these widths;
# beyond that the table scrolls horizontally.
MIN_WIDTHS = {"Target": 20, "Source": 20, "Title": 40, "Author": 16}
# Table group border and padding, the state column and a vertical scrollbar.
TABLE_CHROME = 4 + 3 + 2
CELL_PADDING = 2


def _visible_columns():
    excluded = config.read_config()["exclude-columns"]
    return [c for c in COLUMNS if c.lower() not in excluded]


def _fit_widths(natural, available):
    """Return widths for the shrinkable columns so the row fits available.

    Columns keep their natural width when everything fits; otherwise the
    excess above each column's minimum is cut proportionally.
    """
    floors = {c: min(natural[c], m) for c, m in MIN_WIDTHS.items()
              if c in natural}
    excess = {c: natural[c] - floors[c] for c in floors}
    room = available - sum(floors.values())
    total_excess = sum(excess.values())
    if total_excess <= room:
        return {c: natural[c] for c in floors}
    room = max(0, room)
    return {c: floors[c] + excess[c] * room // total_excess for c in floors}


def pr_cells(pr, repo_name_map):
    """Return the display value of each column for a PR."""
    approvals = str(pr["approval_count"]) if pr["approval_count"] else ""
    if pr.get("my_approved"):
        approvals = f"✓ {approvals}".strip()
    return {
        "#": str(pr["number"]),
        "Repo": repo_name_map.get(pr["repo"], pr["repo"]),
        "Target": pr.get("base_ref") or "",
        "Source": pr.get("head_ref") or "",
        "Title": pr["title"],
        "Author": pr["author"],
        "Stack": pr.get("stack") or "",
        "Rdy": "" if pr.get("draft") else "✓",
        "CI": "✓" if pr["jenkins_approved"] else "",
        "App": approvals,
        "Mrg": "✓" if pr.get("mergeable") else "",
    }


class PrTable(DataTable):
    """DataTable that also highlights the cursor's row with a cell cursor."""
    COMPONENT_CLASSES = DataTable.COMPONENT_CLASSES | {"datatable--cursor-row"}

    # _get_row_style is DataTable-internal; it styles every cell in a row.
    def _get_row_style(self, row_index, base_style):
        style = super()._get_row_style(row_index, base_style)
        if row_index == self.cursor_row and self.has_focus:
            style += self.get_component_styles(
                "datatable--cursor-row").rich_style
        return style

    def watch_cursor_coordinate(self, old, new):
        super().watch_cursor_coordinate(old, new)
        # A cell cursor only repaints the old and new cell, not their rows.
        if old.row != new.row:
            self.refresh_row(old.row)
            self.refresh_row(new.row)

    def on_click(self, event) -> None:
        # DataTable's own click handler has already moved the cursor here.
        if event.chain == 2:
            self.app.action_copy_cell()

    def populate(self, prs, screen_width) -> None:
        """Rebuild the table from prs, fitting the columns to screen_width."""
        columns = _visible_columns()
        repo_name_map = config.read_config().get("repo-name-map")
        self.clear(columns=True)
        self.cursor_type = "cell"
        self.cursor_foreground_priority = "renderable"
        self.zebra_stripes = True
        rows = []
        for pr in prs:
            style = "dim" if pr["state"] == "read" else ""
            state_text = Text(STATE_DISPLAY[pr["state"]],
                              style="dim" if pr["state"] == "read" else "red")
            cells = pr_cells(pr, repo_name_map)
            row_cells = [Text(cells[c], style=style, no_wrap=True,
                              overflow="ellipsis")
                         for c in columns]
            for column, cell in zip(columns, row_cells):
                if column in CHECK_COLUMNS and cell.plain.startswith("✓"):
                    cell.stylize("green", 0, 1)
            rows.append((state_text, row_cells,
                         f"{pr['repo']}#{pr['number']}"))

        natural = {c: max([len(c)] + [cell_len(cells[i].plain)
                                      for _, cells, _ in rows])
                   for i, c in enumerate(columns)}
        fixed = sum(natural[c] for c in columns if c not in MIN_WIDTHS)
        available = (screen_width - TABLE_CHROME
                     - CELL_PADDING * len(columns) - fixed)
        # Cells hold full values; fixed widths make the table crop them.
        widths = _fit_widths(natural, available)
        self.add_column("", key="state")
        for column in columns:
            self.add_column(column, key=column, width=widths.get(column))
        for state_text, row_cells, key in rows:
            self.add_row(state_text, *row_cells, key=key)

    def pr_key(self, row):
        """Return (repo, number) from a specific row's key."""
        row_key, _ = self.coordinate_to_cell_key(Coordinate(row, 0))
        return row_key.value.rsplit("#", 1)

    def dim_row(self, row) -> None:
        """Restyle a row as read."""
        self.update_cell_at(Coordinate(row, STATE_COL),
                            Text(STATE_DISPLAY["read"], style="dim"))
        for col in range(len(self.columns)):
            val = self.get_cell_at(Coordinate(row, col)).copy()
            val.stylize("dim")
            self.update_cell_at(Coordinate(row, col), val)
