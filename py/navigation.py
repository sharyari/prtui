"""Navigation mixin for focus cycling between DataTables."""

from __future__ import annotations
from typing import TYPE_CHECKING

from textual.widgets import DataTable

if TYPE_CHECKING:
    from textual.app import App as _Base
else:
    _Base = object


class NavigationMixin(_Base):
    """Mixin providing focus cycling between DataTables."""

    def _focused_table(self):
        """Return the DataTable that currently has focus, or the first table."""
        if isinstance(self.focused, DataTable):
            return self.focused
        return self.query(DataTable).first()

    def _cycle_focus(self, direction: int) -> None:
        tables = list(self.query(DataTable))
        focused = self.focused
        if isinstance(focused, DataTable) and focused in tables:
            idx = tables.index(focused)
            tables[(idx + direction) % len(tables)].focus()
        else:
            tables[0].focus()

    def action_focus_next_table(self) -> None:
        self._cycle_focus(1)

    def action_focus_prev_table(self) -> None:
        self._cycle_focus(-1)
