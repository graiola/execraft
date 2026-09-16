from execraft.orchestrate.terminal_screen import TerminalScreen


def test_terminal_screen_applies_cursor_redraw_without_duplicate_lines():
    screen = TerminalScreen(rows=10, columns=30)

    screen.feed("status: waiting\rstatus: ready\x1b[K")

    snapshot = screen.snapshot()
    assert snapshot.content == "status: ready"
    assert "waiting" not in snapshot.content


def test_terminal_screen_handles_clear_cursor_and_line_updates():
    screen = TerminalScreen(rows=10, columns=30)
    screen.feed("old\r\ncontent")

    screen.feed("\x1b[2J\x1b[HClaude Code\r\n> prompt")
    screen.feed("\x1b[2K\r> updated")

    snapshot = screen.snapshot()
    assert snapshot.content == "Claude Code\n> updated"
    assert snapshot.cursor_row == 1
    assert snapshot.cursor_column == len("> updated")


def test_terminal_screen_resizes_and_preserves_visible_content():
    screen = TerminalScreen(rows=10, columns=20)
    screen.feed("alpha\r\nbeta")

    screen.resize(rows=12, columns=40)

    snapshot = screen.snapshot()
    assert snapshot.rows == 12
    assert snapshot.columns == 40
    assert snapshot.content == "alpha\nbeta"


def test_terminal_screen_consumes_sgr_and_private_modes():
    screen = TerminalScreen(rows=10, columns=30)

    screen.feed("\x1b[?1049h\x1b[32mREADY\x1b[0m\x1b[?25l")

    snapshot = screen.snapshot()
    assert snapshot.content == "READY"
    assert snapshot.cursor_visible is False
    assert "\x1b" not in snapshot.content


def test_terminal_screen_persists_cursor_only_movement():
    screen = TerminalScreen(rows=10, columns=30)
    screen.feed("ready")
    before = screen.snapshot().revision

    assert screen.feed("\x1b[H") is True
    snapshot = screen.snapshot()
    assert snapshot.revision > before
    assert (snapshot.cursor_row, snapshot.cursor_column) == (0, 0)
