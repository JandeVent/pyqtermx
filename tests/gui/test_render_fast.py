# -*- coding: utf-8 -*-
# Copyright (C) 2018-2026 Connet Information Technology Company, Shanghai.
"""Fast-path render parity: the Cython `_render_fast` run collector
must paint pixel-identical output to the pure-Python `_paint_row`
fallback. The extension is the production path; the fallback is the
reference (and the path used when the extension is not built).

The parity rows exercise every rendition path: defaults, palette,
bold-is-bright, SGR 7 reverse, underline/strike/overline, dim,
box-drawing, block chars, a small square, a wide char + continuation,
RGB, and a hidden cell — plus the DECSCNM ?5 whole-screen reverse and
a selection, which combine with SGR 7 via the XOR.
"""

from __future__ import annotations

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QImage, QPainter

import pyqtermx.render as render_mod
from pyqtermx.render import TerminalRenderer
from pyqtermx.screen import Cell, Row, rgb
from pyqtermx.selection import Selection
from pyqtermx.session import Snapshot


def make_snapshot(*, reverse_video: bool = False) -> Snapshot:
    """A row exercising every rendition path (see module docstring)."""
    cells = [
        Cell("A", -1, -1),  # defaults
        Cell("B", 5, 9),  # palette fg/bg
        Cell("C", 5, 0, bold=True),  # bright fg (5 → 13)
        Cell("D", 12, 0, reverse=True),  # SGR 7
        Cell("E", 4, 5, underline=True, strike=True, overline=True),
        Cell("F", -1, 1, dim=True),  # faint on blue
        Cell("─", 10, 0),  # box-drawing
        Cell("█", 10, 0),  # block
        Cell("⬝", 10, 0),  # small square (vector dot)
        Cell("●", 10, 0),  # circle (vector)
        Cell("□", 10, 0),  # ring square (vector)
        Cell("◆", 10, 0),  # diamond polygon (vector)
        Cell("⠋", 10, 0),  # braille stays in the font (both paths)
        Cell("界", 3, 4), Cell("", 3, 4),  # wide + continuation
        Cell("G", rgb(0x12, 0x34, 0x56), 0),  # RGB fg
        Cell(" ", 2, 6, hidden=True),  # hidden
    ]
    return Snapshot(
        dirty_rows=(0,),
        rows=(Row(cells),),
        scrollback_len=0,
        viewport_offset=0,
        cursor=(-1, 0),
        reverse_video=reverse_video,
        cursor_visible=False,
    )


def render_row(
    renderer: TerminalRenderer, snap: Snapshot, sel: Selection | None
) -> QImage:
    img = QImage(
        round(renderer.cell_w * len(snap.rows[0].cells)),
        renderer.cell_h,
        QImage.Format.Format_RGB32,
    )
    img.fill(Qt.GlobalColor.black)
    painter = QPainter(img)
    try:
        renderer.paint(painter, snap, 1, selection=sel)
    finally:
        painter.end()
    return img


@pytest.fixture
def renderer() -> TerminalRenderer:
    return TerminalRenderer()


def _assert_parity(
    renderer: TerminalRenderer,
    snap: Snapshot,
    sel: Selection | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if render_mod._paint_row_fast is None:
        pytest.skip("Cython _render_fast extension not built")
    fast = render_row(renderer, snap, sel)
    monkeypatch.setattr(render_mod, "_paint_row_fast", None)
    slow = render_row(renderer, snap, sel)
    assert fast.size() == slow.size()
    assert fast.constBits().asstring(fast.sizeInBytes()) == slow.constBits().asstring(
        slow.sizeInBytes()
    )


def test_fast_matches_fallback_pixels(renderer: TerminalRenderer, monkeypatch) -> None:
    _assert_parity(renderer, make_snapshot(), None, monkeypatch)


def test_fast_matches_fallback_reverse_video(
    renderer: TerminalRenderer, monkeypatch
) -> None:
    _assert_parity(renderer, make_snapshot(reverse_video=True), None, monkeypatch)


def test_fast_matches_fallback_selection(
    renderer: TerminalRenderer, monkeypatch
) -> None:
    _assert_parity(renderer, make_snapshot(), Selection(0, 2, 0, 8), monkeypatch)


def test_collect_runs_bright_step_and_swap() -> None:
    """The pure collector folds bold-is-bright into the fg int and
    applies the SGR 7 fg/bg swap — the two C-only behaviors."""
    rf = pytest.importorskip("pyqtermx._render_fast")
    row = Row([Cell("A", 5, 0, bold=True), Cell("B", 12, 0, reverse=True)])
    glyphs = [r for r in rf.collect_runs(row, False, None, codes=render_mod._VECTOR_CODES) if r[0] == 1]
    assert glyphs[0][3] == 13  # bold fg 5 → bright 13
    assert glyphs[1][3] == 0  # reverse: fg becomes the bg 0


def test_collect_runs_classifies_vector_glyphs() -> None:
    """The collector must route every `_VECTOR_GLYPHS` codepoint to an
    individual draw (kind 2) — parity with the Python `_paint_row`."""
    rf = pytest.importorskip("pyqtermx._render_fast")
    cells = [Cell(chr(cp)) for cp in sorted(render_mod._VECTOR_CODES)]
    row = Row(cells)
    runs = rf.collect_runs(row, False, None, codes=render_mod._VECTOR_CODES)
    kinds = {r[0] for r in runs}
    assert kinds == {0, 2}  # background runs + individual draws, no batched text
    assert len(runs) == len(cells) + 1  # one bg run + one draw per cell