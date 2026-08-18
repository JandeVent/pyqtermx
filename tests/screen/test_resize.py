# -*- coding: utf-8 -*-
# Copyright (C) 2018-2026 Connet Information Technology Company, Shanghai.
"""T06 — Resize reflow: re-wrapping lines at a new width (ADR-0003).

The seam: the screen's read API. `resize(lines, cols)` re-wraps every
line's text at the new width instead of truncating; shrinking the height
drops the top lines, keeping the newest content.
"""

from __future__ import annotations

from pyqtermx.screen import Cell

from tests.screen.test_screen import feed_to


def test_widen_merges_wrapped_lines() -> None:
    screen = feed_to("abcdefgh", lines=2, columns=4)
    screen.resize(2, 8)
    assert screen.render() == "abcdefgh\n        "


def test_narrow_rewraps_lines() -> None:
    screen = feed_to("abcdefgh", lines=2, columns=8)
    screen.resize(3, 4)
    assert screen.render() == "abcd\nefgh\n    "


def test_narrow_twice_keeps_content() -> None:
    screen = feed_to("abcdefgh", lines=2, columns=8)
    screen.resize(4, 2)
    assert screen.render() == "ab\ncd\nef\ngh"


def test_shrink_height_keeps_bottom_lines() -> None:
    screen = feed_to("1111\r\n2222\r\n3333", lines=3, columns=4)
    screen.resize(2, 4)
    assert screen.render() == "2222\n3333"


def test_shrink_height_keeps_bottom_blank_lines() -> None:
    """Shrinking the height keeps the *newest* rows — the grid's bottom
    blanks — not the reflowed text above them. The trailing-blank trim
    must not let old content fall into the grid (the resize+scroll
    "old text tints" bug): text at the top of a tall screen, blanks
    below, shrink the height → the grid stays blank and the text goes
    to history."""
    screen = feed_to("\x1b[32mROW-1-GRN\r\n", lines=24, columns=80)
    screen.resize(4, 8)
    assert screen.render() == "        \n        \n        \n        "
    # "ROW-1-GRN" reflows to "ROW-1-GR" + "N" in history, rendition kept.
    assert screen.scrollback_len == 2
    screen.scroll(2)  # view the history
    assert screen.viewport_row(0)[0].fg == 2  # the reflowed text kept its green
    # The history rows are re-padded to the new width — a scrolled-up
    # viewport must render every column (a short row would leave stale
    # pixels in its tail — the "tint fragments" after resize).
    assert len(screen.viewport_row(0).cells) == 8
    assert len(screen.viewport_row(1).cells) == 8


def test_grow_height_pads_blank_lines() -> None:
    screen = feed_to("ab", lines=2, columns=4)
    screen.resize(4, 4)
    assert screen.render() == "ab  \n    \n    \n    "


def test_reflow_preserves_rendition() -> None:
    screen = feed_to("\r\n\x1b[31mred", lines=2, columns=6)
    screen.resize(1, 2)
    line = screen.line(0)
    # "red" reflows to "re"+"d"; only the bottom row survives, so row 0
    # holds "d" (carrying the rendition) plus a blank padding cell.
    assert line[0].data == "d"
    assert line[0].fg == 1
    assert line[1] == Cell.blank()


def test_reflow_preserves_wide_chars() -> None:
    screen = feed_to("你ab", lines=2, columns=6)
    screen.resize(2, 3)
    # 你 occupies cols 0–1 of the new row, 'a' col 2, 'b' wraps.
    assert screen.line(0)[0].data == "你"
    assert screen.line(0)[2].data == "a"
    assert screen.line(1)[0].data == "b"


def test_cursor_clamped_after_resize() -> None:
    screen = feed_to("abcdef", lines=2, columns=6)
    screen.resize(1, 4)
    assert (screen.cursor.x, screen.cursor.y) == (3, 0)
    assert screen.cursor.pending_wrap is False


def test_reflow_preserves_interior_blanks() -> None:
    screen = feed_to("a   b", lines=2, columns=6)
    screen.resize(2, 8)
    assert screen.render() == "a   b   \n        "


def test_reflow_keeps_blank_lines_as_separators() -> None:
    screen = feed_to("ab\n\ncd", lines=4, columns=4)
    screen.resize(4, 8)
    assert screen.render() == "ab      \n        \n  cd    \n        "


def test_reflow_after_combining_mark_does_not_raise() -> None:
    screen = feed_to("e\u0301", lines=2, columns=6)
    screen.resize(2, 4)
    assert screen.line(0)[0].data == "e\u0301"
    assert screen.line(0)[1].data == " "


def test_reflow_after_wide_combining_mark_does_not_raise() -> None:
    screen = feed_to("你\u0301", lines=2, columns=6)
    screen.resize(2, 3)
    assert screen.line(0)[0].data == "你\u0301"


def test_resize_rejects_degenerate_sizes() -> None:
    screen = feed_to("hi")
    for bad in ((0, 4), (4, 0), (-1, 4), (0, 0)):
        try:
            screen.resize(*bad)
        except ValueError:
            continue
        raise AssertionError(f"resize{bad} should have raised ValueError")
