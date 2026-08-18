# cython: language_level=3, boundscheck=False, wraparound=False
# -*- coding: utf-8 -*-
# Copyright (C) 2018-2026 Connet Information Technology Company, Shanghai.
"""Cython fast path for TerminalRenderer._paint_row() — the per-cell loop.

The paint profile showed ~67% of paint time in the Python per-cell loop
of `_paint_row`: three `_color()` calls per cell (dict lookups + QColor
construction), `ord`/`len`/`append` per cell, and ~13M dataclass
attribute reads per 600 donut frames. Moving the loop to C eliminates
the interpreter dispatch while keeping the Qt calls (one fillRect /
drawStaticText per run) in Python — the same run batching the
pure-Python path does, so the two paths paint identical pixels.
Design:
- collect_runs() walks the row's cells in C and emits compact run
  tuples: background runs (kind 0), glyph runs (kind 1), and
  individually-drawn box/block/wide cells (kind 2). Color *ints* are
  compared in C (the bold-is-bright step folded in), so no QColor is
  built per cell; the draw loop resolves one QColor per run.
- paint_row() collects the runs and makes the Qt calls, calling back
  into the renderer for box/block drawing (rare). Glyph runs draw
  through the renderer's QStaticText layout cache (drawText re-lays-out
  every call — the cached layout blits), and setFont/setPen are
  skipped when consecutive runs share them.
- render.py keeps the pure-Python `_paint_row` as the fallback when
  the extension is not built (the same try/except pattern as
  `_screen_fast`).
"""

from cpython.unicode cimport PyUnicode_READ_CHAR

from PyQt6.QtCore import QPointF, QRectF

from .render import (
    DEFAULT_BG,
    DEFAULT_FG,
    _TEXT_FLAGS,
)
from .palette import CUBE_LEVELS, PALETTE16

#: The palette/cube copied into C arrays at import (the single source
#: of truth stays in render.py/palette.py) and the default colors as
#: packed RGB for the SGR 2 dim mix (which resolves -1 to the default).
cdef int[16] _PALETTE_C
cdef int[6] _CUBE_C
cdef int _DFLT_FG_RGB
cdef int _DFLT_BG_RGB

for _i in range(16):
    _PALETTE_C[_i] = <int>PALETTE16[_i]
for _i in range(6):
    _CUBE_C[_i] = <int>CUBE_LEVELS[_i]
_DFLT_FG_RGB = (DEFAULT_FG.red() << 16) | (DEFAULT_FG.green() << 8) | DEFAULT_FG.blue()
_DFLT_BG_RGB = (DEFAULT_BG.red() << 16) | (DEFAULT_BG.green() << 8) | DEFAULT_BG.blue()


cdef inline int _eff(int c, bint bright):
    """A cell color int with the bold-is-bright step applied (palette
    0-7 → 8-15) — mirrors `TerminalRenderer._color`'s `bright` flag."""
    if c >= 0 and c < 8 and bright:
        return c + 8
    return c


cdef inline int _to_rgb(int c, int dflt_rgb):
    """A cell color int → packed 0xRRGGBB (for the SGR 2 dim mix);
    -1 resolves to `dflt_rgb` (the default fg/bg)."""
    if c == -1:
        return dflt_rgb
    if c >= 0x1000000:
        return c
    if c < 16:
        return _PALETTE_C[c]
    if c < 232:
        v = c - 16
        return (
            (_CUBE_C[v // 36] << 16)
            | (_CUBE_C[(v // 6) % 6] << 8)
            | _CUBE_C[v % 6]
        )
    g = 8 + 10 * (c - 232)
    return (g << 16) | (g << 8) | g


cdef inline int _mix(int a, int b):
    """SGR 2: `a` mixed halfway toward `b` — packed RGB with the RGB
    marker set, so the draw loop resolves it through `_color`."""
    return (
        0x1000000
        | (((((a >> 16) & 0xFF) + ((b >> 16) & 0xFF)) // 2) << 16)
        | (((((a >> 8) & 0xFF) + ((b >> 8) & 0xFF)) // 2) << 8)
        | ((((a & 0xFF) + (b & 0xFF)) // 2))
    )


cpdef list collect_runs(row, bint reverse_video, sel_range=None,
                        int dflt_fg_rgb=_DFLT_FG_RGB, int dflt_bg_rgb=_DFLT_BG_RGB,
                        codes=None):
    """The paint runs for one row — the pure, testable hot path.

    `dflt_fg_rgb`/`dflt_bg_rgb` are the packed default colors (the
    renderer's palette, or the module defaults when omitted) used by
    the SGR 2 dim mix to resolve -1 cell colors. `codes` is the
    renderer's vector-glyph codepoint set (`_VECTOR_CODES`) — the
    dense box/block ranges are C checks, the sparse geometric shapes
    and braille fall through to the set.

    Returns a flat list of tuples (the draw loop in `paint_row` walks
    them; render.py's fallback `_paint_row` is the reference):

    - `(0, start, end, color_int, sel)` — a background run: one
      fillRect over [start, end). `sel` is 0 for the bg default,
      1 for the fg default (the SGR 7 / DECSCNM swap).
    - `(1, start, end, fg_int, sel, bold, italic, underline, strike,
      overline, text)` — a glyph run: one drawStaticText over
      [start, end) (the prepared layout, see `_static_text`).
    - `(2, col, cp, data, fg_int, bg_int, fg_sel, bg_sel, bold,
      italic, underline, strike, overline, wide)` — a vector/wide cell
      drawn individually.

    Color ints are compared in C (the bold-is-bright step folded in),
    so no QColor is constructed per cell; the draw loop resolves one
    QColor per run through `TerminalRenderer._color`.
    """
    cdef list cells = row.cells
    cdef Py_ssize_t n = len(cells)
    cdef Py_ssize_t col
    cdef object cell, data
    cdef list runs = []
    cdef list text = []
    cdef int bg_int, fg_int, bg_sel, fg_sel, cp
    cdef int sel_lo = -1, sel_hi = -2
    cdef bint bold, italic, underline, strike, overline, wide
    cdef int run_start, run_bg, run_bg_sel, run_fg, run_fg_sel
    cdef bint run_bold, run_italic, run_ul, run_st, run_ol
    cdef bint has_bg = False, has_fg = False

    if sel_range is not None:
        sel_lo = sel_range[0]
        sel_hi = sel_range[1]

    # -- Pass 1: backgrounds — one run per identical bg. --
    run_start = 0
    run_bg = 0
    run_bg_sel = 0
    for col in range(n):
        cell = cells[col]
        if cell.reverse != reverse_video:  # SGR 7 XOR DECSCNM ?5
            bg_int = cell.fg
            bg_sel = 1
        else:
            bg_int = cell.bg
            bg_sel = 0
        if sel_lo <= col <= sel_hi:
            # Selected: the background is the cell's foreground (the
            # glyph pass swaps the other way — they must agree).
            if cell.reverse == reverse_video:
                bg_int = _eff(cell.fg, cell.bold)
                bg_sel = 1
            else:
                bg_int = _eff(cell.bg, cell.bold)
                bg_sel = 0
        if not has_bg or bg_int != run_bg or bg_sel != run_bg_sel:
            if has_bg:
                runs.append((0, run_start, col, run_bg, run_bg_sel))
            run_bg = bg_int
            run_bg_sel = bg_sel
            run_start = col
            has_bg = True
    if has_bg:
        runs.append((0, run_start, n, run_bg, run_bg_sel))

    # -- Pass 2: glyphs — one run per identical rendition. --
    run_start = 0
    run_fg = 0
    run_fg_sel = 0
    run_bold = False
    run_italic = False
    run_ul = False
    run_st = False
    run_ol = False
    for col in range(n):
        cell = cells[col]
        if cell.hidden or cell.data == "":
            # Continuation cells draw no glyph.
            if has_fg:
                runs.append((1, run_start, col, run_fg, run_fg_sel,
                             run_bold, run_italic, run_ul, run_st, run_ol, text))
                text = []
                has_fg = False
            continue
        bold = cell.bold
        italic = cell.italic
        underline = cell.underline
        strike = cell.strike
        overline = cell.overline
        if cell.reverse != reverse_video:
            # SGR 7 XOR DECSCNM: fg/bg swap — bold-is-bright applies
            # after the swap (xterm behavior).
            fg_int = _eff(cell.bg, bold)
            fg_sel = 0
            bg_int = cell.fg
            bg_sel = 1
        else:
            fg_int = _eff(cell.fg, bold)
            fg_sel = 1
            bg_int = cell.bg
            bg_sel = 0
        if sel_lo <= col <= sel_hi:
            fg_int, bg_int = bg_int, fg_int  # selection renders reversed
            fg_sel, bg_sel = bg_sel, fg_sel
        if cell.dim:
            # SGR 2: fg mixed halfway toward bg (xterm faint).
            fg_int = _mix(
                _to_rgb(fg_int, dflt_fg_rgb if fg_sel == 1 else dflt_bg_rgb),
                _to_rgb(bg_int, dflt_fg_rgb if bg_sel == 1 else dflt_bg_rgb),
            )
        data = cell.data
        if len(data) == 1:
            cp = <int>PyUnicode_READ_CHAR(data, 0)
        else:
            cp = 0
        wide = col + 1 < n and cells[col + 1].data == ""
        # The `cp > 0x7F` gate keeps the ASCII hot path to C
        # comparisons — the set lookup only runs for non-ASCII cells
        # (vector glyphs live above ASCII; U+00B7 is the lowest).
        if (cp > 0x7F and (0x2500 <= cp <= 0x257F or 0x2580 <= cp <= 0x259F or cp in codes)) or wide:
            # Box/block/vector-shape/wide chars break the run and draw
            # individually.
            if has_fg:
                runs.append((1, run_start, col, run_fg, run_fg_sel,
                             run_bold, run_italic, run_ul, run_st, run_ol, text))
                text = []
                has_fg = False
            runs.append((2, col, cp, data, fg_int, bg_int, fg_sel, bg_sel,
                         bold, italic, underline, strike, overline, wide))
            continue
        if (
            has_fg
            and fg_int == run_fg
            and fg_sel == run_fg_sel
            and bold == run_bold
            and italic == run_italic
            and underline == run_ul
            and strike == run_st
            and overline == run_ol
        ):
            text.append(data)
        else:
            if has_fg:
                runs.append((1, run_start, col, run_fg, run_fg_sel,
                             run_bold, run_italic, run_ul, run_st, run_ol, text))
            run_start = col
            run_fg = fg_int
            run_fg_sel = fg_sel
            run_bold = bold
            run_italic = italic
            run_ul = underline
            run_st = strike
            run_ol = overline
            text = [data]
            has_fg = True
    if has_fg:
        runs.append((1, run_start, n, run_fg, run_fg_sel,
                     run_bold, run_italic, run_ul, run_st, run_ol, text))
    return runs


cpdef void paint_row(painter, renderer, int viewport_row, row, bint reverse_video,
                     sel_range=None):
    """Fast path for `TerminalRenderer._paint_row`: collect the runs in
    C, then make the Qt calls — one fillRect per background run, one
    drawStaticText per glyph run, individual draws for vector/wide cells.
    `renderer` supplies the color/font caches, the vector codepoint
    set, and the vector-glyph primitive painter; the painter stays
    open (callers own it)."""
    cdef object codes = renderer._vector_codes
    cdef list runs = collect_runs(
        row, reverse_video, sel_range,
        ((renderer._default_fg.red() << 16) | (renderer._default_fg.green() << 8)
         | renderer._default_fg.blue()),
        ((renderer._default_bg.red() << 16) | (renderer._default_bg.green() << 8)
         | renderer._default_bg.blue()),
        codes,
    )
    cdef double cw = renderer.cell_w
    cdef int ch = renderer.cell_h
    cdef int y0 = viewport_row * ch
    cdef object color = renderer._color
    cdef object font_for = renderer._font_for
    cdef object run, text, data, fg, bg, rect, font, st, off
    #: The painter's current font/pen — consecutive runs usually share
    #: them, and setFont/setPen are Qt state changes (skipped when
    #: unchanged). Reset after an individual draw (kind 2): vector
    #: glyphs change the painter's pen/brush themselves.
    cdef object last_font = None
    cdef object last_pen = None
    cdef int kind, start, end, ci, cj, sel, sel2, cp, col
    cdef bint bold, italic, underline, strike, overline, wide
    cdef object dflt_fg = renderer._default_fg
    cdef object dflt_bg = renderer._default_bg

    for run in runs:
        kind = run[0]
        if kind == 0:
            # Background run: one fillRect.
            start = run[1]
            end = run[2]
            ci = run[3]
            sel = run[4]
            painter.fillRect(
                QRectF(start * cw, y0, (end - start) * cw, ch),
                color(ci, dflt_fg if sel == 1 else dflt_bg),
            )
        elif kind == 1:
            # Glyph run: one drawStaticText from the renderer's
            # prepared-layout cache (see `TerminalRenderer._static_text`
            # — drawText re-lays-out every call, the cached layout
            # blits; the returned offset is the single source for both
            # render paths) plus the underline/strike/overline fills.
            start = run[1]
            end = run[2]
            ci = run[3]
            sel = run[4]
            bold = run[5]
            italic = run[6]
            underline = run[7]
            strike = run[8]
            overline = run[9]
            text = run[10]
            fg = color(ci, dflt_fg if sel == 1 else dflt_bg)
            font = font_for(bold, italic)
            if font is not last_font:
                painter.setFont(font)
                last_font = font
            if fg is not last_pen:
                painter.setPen(fg)
                last_pen = fg
            st, off = renderer._static_text("".join(text), bold, italic)
            painter.drawStaticText(QPointF(start * cw, y0 + off), st)
            if underline:
                painter.fillRect(QRectF(start * cw, y0 + ch - 1, (end - start) * cw, 1), fg)
            if strike:
                painter.fillRect(QRectF(start * cw, y0 + ch // 2, (end - start) * cw, 1), fg)
            if overline:
                painter.fillRect(QRectF(start * cw, y0, (end - start) * cw, 1), fg)
        else:
            # Box/block/wide cell: draw individually.
            col = run[1]
            cp = run[2]
            data = run[3]
            ci = run[4]
            cj = run[5]
            sel = run[6]
            sel2 = run[7]
            bold = run[8]
            italic = run[9]
            underline = run[10]
            strike = run[11]
            overline = run[12]
            wide = run[13]
            fg = color(ci, dflt_fg if sel == 1 else dflt_bg)
            bg = color(cj, dflt_fg if sel2 == 1 else dflt_bg)
            rect = QRectF(col * cw, y0, cw, ch)
            if wide:
                rect.setWidth(2 * cw)
            if 0x2500 <= cp <= 0x257F or 0x2580 <= cp <= 0x259F or cp in codes:
                # Vector glyph: the renderer's primitive table paints it.
                renderer._draw_vector_glyph(painter, rect, cp, fg, bg)
            else:
                painter.setFont(font_for(bold, italic))
                painter.setPen(fg)
                painter.drawText(rect, _TEXT_FLAGS, data)
            # Individual draws change the painter state behind the
            # run loop's back (vector glyphs set pen/brush) — the next
            # run re-establishes its font/pen unconditionally.
            last_font = None
            last_pen = None
            if underline:
                painter.fillRect(QRectF(rect.left(), rect.bottom() - 1, rect.width(), 1), fg)
            if strike:
                painter.fillRect(QRectF(rect.left(), rect.top() + rect.height() // 2, rect.width(), 1), fg)
            if overline:
                painter.fillRect(QRectF(rect.left(), rect.top(), rect.width(), 1), fg)