"""Small, dependency-free VT screen model for dashboard agent terminals.

The dashboard does not need to emulate every historical terminal feature.  It
needs a stable representation of modern CLI TUIs (Claude Code, Codex and
OpenCode) without turning cursor redraws into thousands of duplicated transcript
lines.  :class:`TerminalScreen` implements the common ECMA-48 controls emitted
by those tools and deliberately ignores unsupported styling commands.

The model is kept server-side so terminal state remains available after the
browser is closed and no third-party JavaScript terminal bundle is required.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TerminalSnapshot:
    """Serializable terminal surface returned to the dashboard."""

    content: str
    rows: int
    columns: int
    cursor_row: int
    cursor_column: int
    cursor_visible: bool
    revision: int

    def as_mapping(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "rows": self.rows,
            "columns": self.columns,
            "cursor_row": self.cursor_row,
            "cursor_column": self.cursor_column,
            "cursor_visible": self.cursor_visible,
            "revision": self.revision,
        }


class TerminalScreen:
    """Interpret the practical VT subset used by interactive coding agents.

    The implementation intentionally focuses on screen semantics rather than
    colours.  It supports cursor movement, erasing, insertion/deletion,
    scrolling, alternate-screen resets and terminal resizing.  Unknown control
    sequences are consumed safely and do not leak raw escape bytes into the UI.
    """

    def __init__(self, *, rows: int = 40, columns: int = 120) -> None:
        self.rows = self._bounded(rows, 10, 300)
        self.columns = self._bounded(columns, 20, 500)
        self._cells = self._blank_screen(self.rows, self.columns)
        self.cursor_row = 0
        self.cursor_column = 0
        self.cursor_visible = True
        self._saved_cursor = (0, 0)
        self._scroll_top = 0
        self._scroll_bottom = self.rows - 1
        self._state = "normal"
        self._sequence = ""
        self._osc_escaped = False
        self._wrap_pending = False
        self.revision = 0

    def feed(self, text: str) -> bool:
        """Consume terminal output and return whether the visible screen changed."""

        changed_before = self.revision
        for character in str(text):
            cursor_before = (
                self.cursor_row,
                self.cursor_column,
                self.cursor_visible,
            )
            revision_before = self.revision
            if self._state == "normal":
                self._consume_normal(character)
            elif self._state == "escape":
                self._consume_escape(character)
            elif self._state == "csi":
                self._consume_csi(character)
            elif self._state == "osc":
                self._consume_osc(character)
            elif self._state == "charset":
                self._state = "normal"
            cursor_after = (
                self.cursor_row,
                self.cursor_column,
                self.cursor_visible,
            )
            if cursor_after != cursor_before and self.revision == revision_before:
                self._touch()
        return self.revision != changed_before

    def resize(self, *, rows: int, columns: int) -> None:
        """Resize while retaining the visible top-left terminal content."""

        new_rows = self._bounded(rows, 10, 300)
        new_columns = self._bounded(columns, 20, 500)
        if (new_rows, new_columns) == (self.rows, self.columns):
            return
        resized = self._blank_screen(new_rows, new_columns)
        for row in range(min(self.rows, new_rows)):
            for column in range(min(self.columns, new_columns)):
                resized[row][column] = self._cells[row][column]
        self.rows = new_rows
        self.columns = new_columns
        self._cells = resized
        self.cursor_row = min(self.cursor_row, self.rows - 1)
        self.cursor_column = min(self.cursor_column, self.columns - 1)
        self._scroll_top = 0
        self._scroll_bottom = self.rows - 1
        self._wrap_pending = False
        self._touch()

    def snapshot(self) -> TerminalSnapshot:
        lines = ["".join(row).rstrip() for row in self._cells]
        while lines and not lines[-1]:
            lines.pop()
        return TerminalSnapshot(
            content="\n".join(lines),
            rows=self.rows,
            columns=self.columns,
            cursor_row=self.cursor_row,
            cursor_column=self.cursor_column,
            cursor_visible=self.cursor_visible,
            revision=self.revision,
        )

    def _consume_normal(self, character: str) -> None:
        code = ord(character)
        if character == "\x1b":
            self._state = "escape"
            self._sequence = ""
        elif character == "\r":
            self.cursor_column = 0
            self._wrap_pending = False
        elif character in {"\n", "\x0b", "\x0c"}:
            self._linefeed()
        elif character == "\b":
            self.cursor_column = max(0, self.cursor_column - 1)
            self._wrap_pending = False
        elif character == "\t":
            target = min(self.columns - 1, ((self.cursor_column // 8) + 1) * 8)
            self.cursor_column = target
            self._wrap_pending = False
        elif code < 32 or code == 127:
            return
        else:
            self._write(character)

    def _consume_escape(self, character: str) -> None:
        self._state = "normal"
        if character == "[":
            self._state = "csi"
            self._sequence = ""
        elif character == "]":
            self._state = "osc"
            self._sequence = ""
            self._osc_escaped = False
        elif character in {"(", ")", "*", "+"}:
            self._state = "charset"
        elif character == "7":
            self._saved_cursor = (self.cursor_row, self.cursor_column)
        elif character == "8":
            self.cursor_row, self.cursor_column = self._saved_cursor
            self._clamp_cursor()
        elif character == "D":
            self._linefeed()
        elif character == "M":
            self._reverse_linefeed()
        elif character == "E":
            self.cursor_column = 0
            self._linefeed()
        elif character == "c":
            self._reset()

    def _consume_csi(self, character: str) -> None:
        if "@" <= character <= "~":
            sequence = self._sequence
            self._sequence = ""
            self._state = "normal"
            self._handle_csi(sequence, character)
            return
        if len(self._sequence) < 128:
            self._sequence += character
        else:
            self._sequence = ""
            self._state = "normal"

    def _consume_osc(self, character: str) -> None:
        if character == "\x07":
            self._state = "normal"
            self._sequence = ""
            return
        if self._osc_escaped:
            if character == "\\":
                self._state = "normal"
                self._sequence = ""
                self._osc_escaped = False
                return
            self._osc_escaped = False
        if character == "\x1b":
            self._osc_escaped = True
        elif len(self._sequence) < 4096:
            self._sequence += character

    def _handle_csi(self, raw: str, final: str) -> None:
        private = raw.startswith("?")
        cleaned = raw[1:] if private else raw
        params = self._parameters(cleaned)
        first = params[0] if params else 0
        amount = max(1, first or 1)

        if final == "A":
            self.cursor_row = max(self._scroll_top, self.cursor_row - amount)
        elif final == "B":
            self.cursor_row = min(self._scroll_bottom, self.cursor_row + amount)
        elif final == "C":
            self.cursor_column = min(self.columns - 1, self.cursor_column + amount)
        elif final == "D":
            self.cursor_column = max(0, self.cursor_column - amount)
        elif final == "E":
            self.cursor_row = min(self._scroll_bottom, self.cursor_row + amount)
            self.cursor_column = 0
        elif final == "F":
            self.cursor_row = max(self._scroll_top, self.cursor_row - amount)
            self.cursor_column = 0
        elif final in {"G", "`"}:
            self.cursor_column = min(self.columns - 1, max(0, amount - 1))
        elif final in {"H", "f"}:
            row = (params[0] if params else 1) or 1
            column = (params[1] if len(params) > 1 else 1) or 1
            self.cursor_row = min(self.rows - 1, max(0, row - 1))
            self.cursor_column = min(self.columns - 1, max(0, column - 1))
        elif final == "d":
            self.cursor_row = min(self.rows - 1, max(0, amount - 1))
        elif final == "J":
            self._erase_display(first)
        elif final == "K":
            self._erase_line(first)
        elif final == "P":
            self._delete_characters(amount)
        elif final == "@":
            self._insert_characters(amount)
        elif final == "X":
            self._erase_characters(amount)
        elif final == "L":
            self._insert_lines(amount)
        elif final == "M":
            self._delete_lines(amount)
        elif final == "S":
            self._scroll_up(amount)
        elif final == "T":
            self._scroll_down(amount)
        elif final == "r":
            top = (params[0] if params else 1) or 1
            bottom = (params[1] if len(params) > 1 else self.rows) or self.rows
            if 1 <= top < bottom <= self.rows:
                self._scroll_top = top - 1
                self._scroll_bottom = bottom - 1
                self.cursor_row = self._scroll_top
                self.cursor_column = 0
        elif final == "s":
            self._saved_cursor = (self.cursor_row, self.cursor_column)
        elif final == "u":
            self.cursor_row, self.cursor_column = self._saved_cursor
            self._clamp_cursor()
        elif final in {"h", "l"} and private:
            enabled = final == "h"
            for mode in params:
                if mode == 25:
                    if self.cursor_visible != enabled:
                        self.cursor_visible = enabled
                        self._touch()
                elif mode in {47, 1047, 1049} and enabled:
                    self._clear_screen()
                    self.cursor_row = 0
                    self.cursor_column = 0
        # SGR (m), device reports and unsupported private modes intentionally do
        # not affect the plain-text screen model.
        self._wrap_pending = False
        self._clamp_cursor()

    def _write(self, character: str) -> None:
        if self._wrap_pending:
            self.cursor_column = 0
            self._linefeed()
            self._wrap_pending = False
        if self._cells[self.cursor_row][self.cursor_column] != character:
            self._cells[self.cursor_row][self.cursor_column] = character
            self._touch()
        if self.cursor_column >= self.columns - 1:
            self._wrap_pending = True
        else:
            self.cursor_column += 1

    def _linefeed(self) -> None:
        if self.cursor_row == self._scroll_bottom:
            self._scroll_up(1)
        else:
            self.cursor_row = min(self.rows - 1, self.cursor_row + 1)
        self._wrap_pending = False

    def _reverse_linefeed(self) -> None:
        if self.cursor_row == self._scroll_top:
            self._scroll_down(1)
        else:
            self.cursor_row = max(0, self.cursor_row - 1)
        self._wrap_pending = False

    def _erase_display(self, mode: int) -> None:
        if mode in {2, 3}:
            self._clear_screen()
        elif mode == 1:
            for row in range(0, self.cursor_row):
                self._cells[row] = [" "] * self.columns
            self._cells[self.cursor_row][: self.cursor_column + 1] = [" "] * (
                self.cursor_column + 1
            )
            self._touch()
        else:
            self._cells[self.cursor_row][self.cursor_column :] = [" "] * (
                self.columns - self.cursor_column
            )
            for row in range(self.cursor_row + 1, self.rows):
                self._cells[row] = [" "] * self.columns
            self._touch()

    def _erase_line(self, mode: int) -> None:
        if mode == 2:
            self._cells[self.cursor_row] = [" "] * self.columns
        elif mode == 1:
            self._cells[self.cursor_row][: self.cursor_column + 1] = [" "] * (
                self.cursor_column + 1
            )
        else:
            self._cells[self.cursor_row][self.cursor_column :] = [" "] * (
                self.columns - self.cursor_column
            )
        self._touch()

    def _delete_characters(self, amount: int) -> None:
        row = self._cells[self.cursor_row]
        count = min(amount, self.columns - self.cursor_column)
        del row[self.cursor_column : self.cursor_column + count]
        row.extend([" "] * count)
        self._touch()

    def _insert_characters(self, amount: int) -> None:
        row = self._cells[self.cursor_row]
        count = min(amount, self.columns - self.cursor_column)
        row[self.cursor_column : self.cursor_column] = [" "] * count
        del row[self.columns :]
        self._touch()

    def _erase_characters(self, amount: int) -> None:
        count = min(amount, self.columns - self.cursor_column)
        self._cells[self.cursor_row][self.cursor_column : self.cursor_column + count] = [
            " "
        ] * count
        self._touch()

    def _insert_lines(self, amount: int) -> None:
        if not self._scroll_top <= self.cursor_row <= self._scroll_bottom:
            return
        for _ in range(min(amount, self._scroll_bottom - self.cursor_row + 1)):
            self._cells.insert(self.cursor_row, [" "] * self.columns)
            del self._cells[self._scroll_bottom + 1]
        self._touch()

    def _delete_lines(self, amount: int) -> None:
        if not self._scroll_top <= self.cursor_row <= self._scroll_bottom:
            return
        for _ in range(min(amount, self._scroll_bottom - self.cursor_row + 1)):
            del self._cells[self.cursor_row]
            self._cells.insert(self._scroll_bottom, [" "] * self.columns)
        self._touch()

    def _scroll_up(self, amount: int) -> None:
        count = min(amount, self._scroll_bottom - self._scroll_top + 1)
        for _ in range(count):
            del self._cells[self._scroll_top]
            self._cells.insert(self._scroll_bottom, [" "] * self.columns)
        self._touch()

    def _scroll_down(self, amount: int) -> None:
        count = min(amount, self._scroll_bottom - self._scroll_top + 1)
        for _ in range(count):
            del self._cells[self._scroll_bottom]
            self._cells.insert(self._scroll_top, [" "] * self.columns)
        self._touch()

    def _clear_screen(self) -> None:
        self._cells = self._blank_screen(self.rows, self.columns)
        self._touch()

    def _reset(self) -> None:
        self._clear_screen()
        self.cursor_row = 0
        self.cursor_column = 0
        self.cursor_visible = True
        self._saved_cursor = (0, 0)
        self._scroll_top = 0
        self._scroll_bottom = self.rows - 1
        self._wrap_pending = False

    def _clamp_cursor(self) -> None:
        self.cursor_row = min(self.rows - 1, max(0, self.cursor_row))
        self.cursor_column = min(self.columns - 1, max(0, self.cursor_column))

    def _touch(self) -> None:
        self.revision += 1

    @staticmethod
    def _parameters(raw: str) -> list[int]:
        if not raw:
            return []
        result: list[int] = []
        for item in raw.split(";"):
            try:
                result.append(int(item) if item else 0)
            except ValueError:
                result.append(0)
        return result

    @staticmethod
    def _blank_screen(rows: int, columns: int) -> list[list[str]]:
        return [[" "] * columns for _ in range(rows)]

    @staticmethod
    def _bounded(value: int, minimum: int, maximum: int) -> int:
        return max(minimum, min(maximum, int(value)))
