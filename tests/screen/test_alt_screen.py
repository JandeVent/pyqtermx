# -*- coding: utf-8 -*-
# Copyright (C) 2018-2026 Connet Information Technology Company, Shanghai.
"""T02 — alternate screen (?47/?1047/?1048/?1049) — ADR-0004.

The semantics are xterm.js-verbatim; the first five tests are ported
1:1 from xterm.js `InputHandler.test.ts` (the "alt screen buffer"
describe block) through the feed seam. What the tests pin:

- 47/1047 switch to a fresh alternate screen and back, the cursor
  position carrying both ways, the rendition shared;
- 1048 saves/restores the cursor only — no switch;
- 1049 saves the cursor, enters the alternate screen, and on exit
  clears it and restores;
- the DECSC slot is per-screen (a save inside the alt screen restores
  there later, independently);
- entry fills the alt screen with the erase fill (cursor's bg);
- resize reflows both grids under ADR-0003.
"""

from pyqtermx.screen import Screen

from .test_screen import feed_to, make_screen, make_screen


def _row_text(screen: Screen, y: int) -> str:
    return "".join(cell.data for cell in screen.line(y).cells).rstrip(" ")


def test_47_switches_to_alt_screen_and_back() -> None:
    """xterm.js: DECSET/DECRST 47 — JUNK goes to the alt buffer, TEST
    lands back on the main buffer at the carried-back cursor, red."""
    screen = feed_to("\x1b[?47h\r\n\x1b[31mJUNK\x1b[?47lTEST")
    assert _row_text(screen, 0) == ""
    assert _row_text(screen, 1) == "    TEST"
    assert screen.line(1)[4].fg == 1  # red — rendition shared


def test_alt_screen_flag_tracks_the_active_grid() -> None:
    """The `alt_screen` property — the widget's wheel policy input:
    the alternate screen has no scrollback, so wheel pages the app."""
    parser, _emulator, screen = make_screen()
    assert not screen.alt_screen
    parser.feed("\x1b[?1049h")
    parser.flush()
    assert screen.alt_screen
    parser.feed("\x1b[?1049l")
    parser.flush()
    assert not screen.alt_screen


def test_1047_switches_to_alt_screen_and_back() -> None:
    """xterm.js: DECSET/DECRST 1047 — same as 47."""
    screen = feed_to("\x1b[?1047h\r\n\x1b[31mJUNK\x1b[?1047lTEST")
    assert _row_text(screen, 0) == ""
    assert _row_text(screen, 1) == "    TEST"
    assert screen.line(1)[4].fg == 1  # red


def test_1048_saves_and_restores_cursor_only() -> None:
    """xterm.js: DECSET/DECRST 1048 — no switch: JUNK and TEST share
    the main buffer; the restore brings back the default rendition."""
    screen = feed_to("\x1b[?1048h\r\n\x1b[31mJUNK\x1b[?1048lTEST")
    assert _row_text(screen, 0) == "TEST"
    assert _row_text(screen, 1) == "JUNK"
    assert screen.line(0)[0].fg == -1  # default — restored
    assert screen.line(1)[0].fg == 1  # red — untouched


def test_1049_saves_and_restores_cursor_around_switch() -> None:
    """xterm.js: DECSET/DECRST 1049 — JUNK goes to the alt buffer; the
    exit restores the saved cursor and rendition, so TEST lands at the
    saved position in the main buffer, default."""
    screen = feed_to("\x1b[?1049h\r\n\x1b[31mJUNK\x1b[?1049lTEST")
    assert _row_text(screen, 0) == "TEST"
    assert _row_text(screen, 1) == ""
    assert screen.line(0)[0].fg == -1  # default — restored


def test_1049_maintains_saved_cursor_for_alt_buffer() -> None:
    """xterm.js: the DECSC slot is per-screen — `CSI s` inside the alt
    screen saves to the alt's own slot, and `CSI u` in a later alt
    session restores it (position and rendition)."""
    parser, _emulator, screen = make_screen()
    parser.feed("\x1b[?1049h\r\n\x1b[31m\x1b[s\x1b[?1049lTEST")
    parser.flush()
    assert _row_text(screen, 0) == "TEST"
    assert screen.line(0)[0].fg == -1  # default — the normal slot restored
    parser.feed("\x1b[?1049h\x1b[uTEST")
    parser.flush()
    assert _row_text(screen, 1) == "TEST"  # the alt's own saved position
    assert screen.line(1)[0].fg == 1  # red — the alt's own saved rendition


def test_1049_clears_alt_buffer_with_erase_attributes() -> None:
    """xterm.js: entry fills the alt buffer with the erase fill —
    the cursor's current background color (42 = green, palette 2)."""
    screen = feed_to("\x1b[42m\x1b[?1049h")
    assert screen.line(20)[10].bg == 2


def test_alt_screen_fresh_on_entry_after_exit() -> None:
    """Clear-on-exit: content written to the alt screen is gone when
    the screen is entered again."""
    screen = feed_to("\x1b[?1047hJUNK\x1b[?1047l\x1b[?1047h")
    assert _row_text(screen, 0) == ""


def test_cursor_position_carries_both_ways() -> None:
    """The cursor position travels with the switch: main (2,0) → alt
    prints at (2,0) → exit carries (3,0) back to main."""
    screen = feed_to("ab\x1b[?1047hX\x1b[?1047lY")
    assert _row_text(screen, 0) == "ab Y"


def test_resize_reflows_both_grids() -> None:
    """ADR-0003 + ADR-0004: resize reflows the normal grid and the
    alternate grid independently — wrapped rows re-join on the alt
    grid too, and the main grid is preserved underneath. Content sits
    at the bottom of each grid so it survives the shrink (the grid
    keeps its newest rows)."""
    parser, _emulator, screen = make_screen(6, 10)
    parser.feed("\r\n\r\n\r\nabc\r\ndef\x1b[?1047h\r\n\r\n\r\n\r\nuvwxyz")
    parser.flush()
    screen.resize(4, 5)
    # Still in the alt screen: its grid reflowed at the new width.
    assert _row_text(screen, 0) == "uvwxy"
    assert not screen.line(0).wrapped
    assert _row_text(screen, 1) == "z"
    assert screen.line(1).wrapped
    parser.feed("\x1b[?1047l")
    lines = screen.render().split("\n")
    assert [line.rstrip() for line in lines[:2]] == ["abc", "def"]
    assert [line.rstrip() for line in lines[2:]] == ["", ""]


def test_redundant_enter_preserves_alt_content() -> None:
    """A DECSET while already in the alt screen is a no-op (xterm.js
    activateAltBuffer early-returns): the alt content survives a
    redundant or nested 47/1047/1049."""
    screen = feed_to("\x1b[?47hJUNK\x1b[?47h\x1b[?1049h")
    assert _row_text(screen, 0) == "JUNK"  # not wiped by re-entry


def test_leave_when_already_normal_is_a_noop() -> None:
    """DECRST on the normal screen does nothing (xterm.js
    activateNormalBuffer early-returns)."""
    screen = feed_to("abc\x1b[?47l\x1b[?1049l")
    assert _row_text(screen, 0) == "abc"


def test_1048_inside_alt_saves_the_alt_slot() -> None:
    """`?1048h` inside the alt screen saves to the alt's own DECSC
    slot, which a later alt session restores (ADR-0004)."""
    parser, _emulator, screen = make_screen()
    parser.feed("\x1b[?1049h\r\n\x1b[31m\x1b[?1048h\x1b[?1049l")
    parser.flush()
    parser.feed("\x1b[?1049h\x1b[?1048lTEST")
    parser.flush()
    assert _row_text(screen, 1) == "TEST"  # the alt slot's position
    assert screen.line(1)[0].fg == 1  # the alt slot's rendition
