# -*- coding: utf-8 -*-
# Copyright (C) 2018-2026 Connet Information Technology Company, Shanghai.
"""Phase 4 — Scrollback & retention (ADR-0006).

History rows live on the normal screen above the grid; only full-screen
scrolls feed them; the cap drops oldest-first; the alt screen has none;
ED3 clears; resize reflows history + grid as one stream; the viewport
read API (viewport_row, scroll commands) is model state.

The seam: the screen's read API (line(y), render(), viewport_row,
viewport_offset, scrollback_len) plus the model-level scroll commands —
headless, no pty.
"""

from __future__ import annotations

from pyqtermx.emulator import Emulator
from pyqtermx.parser import Parser
from pyqtermx.screen import Row, Screen

from tests.screen.test_screen import feed_to


def feed_scrollback(text: str, limit: int = 1000, lines: int = 24, columns: int = 80) -> Screen:
    """Feed through the full pipeline with a custom scrollback cap."""
    screen = Screen(lines=lines, columns=columns, scrollback_limit=limit)
    parser = Parser(Emulator(screen))
    parser.feed(text)
    parser.flush()
    return screen


def text(row: Row) -> str:
    """A viewport row as stripped text (rows are padded to the width)."""
    return "".join(c.data for c in row).strip()


# -- Entry: full-screen scrolling pushes rows into history ---------------

def test_line_feed_at_bottom_enters_history() -> None:
    screen = feed_to("1\r\n2\r\n3\r\n4", lines=3, columns=4)
    # One row scrolled into history; the viewport is still live (0).
    assert screen.scrollback_len == 1
    assert screen.viewport_offset == 0
    assert text(screen.viewport_row(0)) == "2"
    screen.scroll(1)
    assert screen.viewport_offset == 1
    # Scrolled up, the history row becomes visible first.
    assert text(screen.viewport_row(0)) == "1"


def test_scrollback_grows_with_output() -> None:
    screen = feed_to("".join(f"{i}\r\n" for i in range(1, 21)), lines=5, columns=4)
    assert screen.scrollback_len == 16
    assert screen.viewport_offset == 0


def test_viewport_row_maps_history_then_grid() -> None:
    screen = feed_to("1\r\n2\r\n3\r\n4", lines=3, columns=4)
    screen.scroll(1)
    rows = [text(screen.viewport_row(k)) for k in range(3)]
    assert rows == ["1", "2", "3"]


def test_viewport_row_at_bottom_shows_grid() -> None:
    screen = feed_to("1\r\n2\r\n3", lines=3, columns=4)
    rows = [text(screen.viewport_row(k)) for k in range(3)]
    assert rows == ["1", "2", "3"]


def test_su_full_screen_feeds_history() -> None:
    screen = feed_to("1\r\n2\r\n3\r\n4\x1b[S", lines=3, columns=4)
    # SU over the full screen scrolls "2" off the top — into history
    # on top of the row the line feed already pushed.
    assert screen.scrollback_len == 2
    screen.scroll(2)
    assert screen.viewport_row(0)[0].data == "1"


def test_narrowed_region_scroll_discards() -> None:
    # The region is narrowed before the scrolling feeds happen.
    screen = feed_to("1\r\n2\r\n3\r\n4\x1b[2;4r5\r\n6", lines=4, columns=4)
    assert screen.scrollback_len == 0


def test_ri_at_top_discards_no_history_restore() -> None:
    screen = feed_to("1\r\n2\r\n3\r\n4", lines=3, columns=4)
    assert screen.scrollback_len == 1
    screen.reverse_index()  # RI at the top of the full screen
    assert screen.scrollback_len == 1  # no restore from history (spec)


# -- Cap ---------------------------------------------------------------

def test_cap_drops_oldest() -> None:
    screen = feed_scrollback("".join(f"{i}\r\n" for i in range(1, 9)), limit=3, lines=3, columns=4)
    assert screen.scrollback_len == 3
    screen.scroll(3)
    assert screen.viewport_row(0)[0].data == "4"


def test_zero_limit_disables_scrollback() -> None:
    screen = feed_scrollback("1\r\n2\r\n3\r\n4", limit=0, lines=3, columns=4)
    assert screen.scrollback_len == 0


# -- Viewport commands ---------------------------------------------------

def test_scroll_clamps_to_history() -> None:
    screen = feed_to("1\r\n2\r\n3\r\n4", lines=3, columns=4)
    screen.scroll(99)
    assert screen.viewport_offset == 1
    screen.scroll(-99)
    assert screen.viewport_offset == 0


def test_scroll_to_bottom_resets_offset() -> None:
    screen = feed_to("1\r\n2\r\n3\r\n4", lines=3, columns=4)
    screen.scroll(1)
    assert screen.viewport_offset == 1
    screen.scroll_to_bottom()
    assert screen.viewport_offset == 0


def test_scroll_commands_never_touch_grid() -> None:
    screen = feed_to("1\r\n2\r\n3\r\n4", lines=3, columns=4)
    before = screen.render()
    screen.scroll(1)
    screen.scroll_to_bottom()
    assert screen.render() == before


# -- Alt screen exclusion (ADR-0006) ------------------------------------

def test_alt_screen_has_no_scrollback() -> None:
    screen = feed_to("1\r\n2\r\n3\r\n4\x1b[?1049h", lines=3, columns=4)
    assert screen.scrollback_len == 0
    assert screen.viewport_offset == 0


def test_scrolling_in_alt_screen_leaves_history_untouched() -> None:
    screen = feed_to("1\r\n2\r\n3\r\n4", lines=3, columns=4)
    history = screen.scrollback_len
    parser = Parser(Emulator(screen))
    parser.feed("\x1b[?1049h")
    parser.flush()
    parser.feed("a\r\nb\r\nc\r\nd")
    parser.flush()
    # In the alt screen the scrollback API reads empty (ADR-0006)…
    assert screen.scrollback_len == 0
    assert screen.viewport_offset == 0
    parser.feed("\x1b[?1049l")
    parser.flush()
    # …and leaving it: history intact, viewport still live.
    assert screen.scrollback_len == history
    assert screen.viewport_offset == 0


# -- Erase interactions ---------------------------------------------------

def test_ed3_clears_history_and_snaps_viewport() -> None:
    screen = feed_to("1\r\n2\r\n3\r\n4\x1b[3J", lines=3, columns=4)
    assert screen.scrollback_len == 0
    assert screen.viewport_offset == 0
    # The grid is untouched by ED3.
    assert screen.render().split("\n")[0].strip() == "2"


def test_ed1_ed2_decaln_leave_history() -> None:
    screen = feed_to("1\r\n2\r\n3\r\n4", lines=3, columns=4)
    assert screen.scrollback_len == 1
    parser = Parser(Emulator(screen))
    parser.feed("\x1b[2J")
    parser.flush()
    assert screen.scrollback_len == 1
    parser.feed("\x1b[1J")
    parser.flush()
    assert screen.scrollback_len == 1
    parser.feed("\x1b#8")
    parser.flush()
    assert screen.scrollback_len == 1


# -- One-stream reflow (ADR-0006) ----------------------------------------

def test_resize_reflows_history_and_grid_together() -> None:
    # "abcdefgh" wraps to abcd/efgh; the feed pushes abcd into history.
    # Widening must re-join the pair across the boundary.
    screen = feed_to("abcdefgh\r\nx", lines=2, columns=4)
    assert screen.scrollback_len == 1
    screen.scroll(1)
    assert screen.viewport_row(0)[0].data == "a"
    screen.resize(2, 8)
    assert screen.scrollback_len == 0
    assert text(screen.viewport_row(0)) == "abcdefgh"


def test_resize_narrow_keeps_newest_grid_and_history() -> None:
    screen = feed_to("".join(f"{i}\r\n" for i in range(1, 7)), lines=4, columns=4)
    # History 1,2,3; grid 4,5,6,blank.
    assert screen.scrollback_len == 3
    screen.resize(2, 4)
    # Stream reflows to 7 rows (6 content + the grid's bottom blank);
    # the grid keeps its newest 2 rows — "6" and the bottom blank — and
    # the rest is history.
    assert screen.scrollback_len == 5
    rows = [text(screen.viewport_row(k)) for k in range(2)]
    assert rows == ["6", ""]
    assert screen.viewport_offset == 0  # still live


def test_resize_grows_height_pulls_history_into_grid() -> None:
    # Growing the height shows more of the stream: history shrinks.
    screen = feed_to("".join(f"{i}\r\n" for i in range(1, 7)), lines=4, columns=4)
    assert screen.scrollback_len == 3
    screen.resize(6, 4)
    assert screen.scrollback_len == 0
    assert screen.render().split("\n")[0].strip() == "1"


def test_resize_clamps_offset() -> None:
    screen = feed_to("".join(f"{i}\r\n" for i in range(1, 7)), lines=3, columns=4)
    # History 1,2,3; grid 4,5,6.
    screen.scroll(2)
    assert screen.viewport_offset == 2
    screen.resize(4, 4)  # history shrinks to 2, offset clamps
    assert screen.viewport_offset == 2
    screen.resize(5, 4)  # history shrinks to 1, offset clamps again
    assert screen.viewport_offset == 1


# -- Dirty rows (the transport seam, ADR-0005) ---------------------------

def test_dirty_rows_track_print_and_clear() -> None:
    screen = Screen(lines=3, columns=4)
    screen.print("abc")
    assert screen.take_dirty_rows() == {0}
    assert screen.take_dirty_rows() == set()


def test_dirty_rows_mark_scrolled_region() -> None:
    screen = feed_to("1\r\n2\r\n3\r\n4", lines=3, columns=4)
    dirty = screen.take_dirty_rows()
    assert dirty == {0, 1, 2}


def test_dirty_rows_mark_erase() -> None:
    screen = Screen(lines=3, columns=4)
    screen.print("abc")
    screen.take_dirty_rows()
    screen.erase_in_display(2)
    assert screen.take_dirty_rows() == {0, 1, 2}
