# -*- coding: utf-8 -*-
# Copyright (C) 2018-2026 Connet Information Technology Company, Shanghai.
"""T08 — snapshot → pixels: the renderer paints frozen snapshot rows
into a QImage backing store. Pure Qt (offscreen): assertions read pixel
colors at cell positions — the widget blits the same image untouched.

Cell color contract (screen.py): -1 = default, 0–255 palette (0–15
xterm ANSI, 16–231 cube, 232–255 grayscale), >= 0x1000000 = RGB.
"""

from __future__ import annotations

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont, QImage, QPainter

from pyqtermx.render import DEFAULT_BG, DEFAULT_FG, TerminalRenderer
from pyqtermx.screen import Cell, Row, rgb
from pyqtermx.selection import Selection
from pyqtermx.session import Snapshot


def make_row(*cells: Cell) -> Row:
    return Row(list(cells))


def blank_cell() -> Cell:
    return Cell.blank()


def snapshot(
    rows: list[Row],
    *,
    cursor: tuple[int, int] = (-1, 0),
    viewport_offset: int = 0,
    cursor_visible: bool = True,
) -> Snapshot:
    return Snapshot(
        dirty_rows=tuple(range(len(rows))),
        rows=tuple(rows),
        scrollback_len=0,
        viewport_offset=viewport_offset,
        cursor=cursor,
        cursor_visible=cursor_visible,
    )


@pytest.fixture
def renderer() -> TerminalRenderer:
    return TerminalRenderer()


@pytest.fixture
def image(renderer: TerminalRenderer) -> QImage:
    img = QImage(
        round(2 * renderer.cell_w),
        1 * renderer.cell_h,
        QImage.Format.Format_RGB32,
    )
    img.fill(Qt.GlobalColor.black)
    return img


def cell_pixel(image: QImage, renderer: TerminalRenderer, col: int, row: int = 0) -> QColor:
    """The pixel at a cell's center (glyphs rarely reach there)."""
    return image.pixelColor(
        round(renderer.cell_w * col + renderer.cell_w / 2),
        renderer.cell_h * row + renderer.cell_h // 2,
    )


def cell_has_color(
    image: QImage, renderer: TerminalRenderer, col: int, color: QColor, row: int = 0
) -> bool:
    for y in range(renderer.cell_h * row, renderer.cell_h * (row + 1)):
        for x in range(round(renderer.cell_w * col), round(renderer.cell_w * (col + 1))):
            if image.pixelColor(x, y) == color:
                return True
    return False


def cell_has_color_approx(
    image: QImage,
    renderer: TerminalRenderer,
    col: int,
    color: QColor,
    row: int = 0,
    tol: int = 150,
) -> bool:
    """Any pixel within `tol` per channel of `color` — glyphs are
    font-antialiased (no pixel is exactly the pure color on some
    machines), so glyph assertions compare approximately; background
    fills (fillRect) stay exact via `cell_has_color`/`cell_pixel`. The
    tolerance is well below the default-background distance (~220), so
    a plain cell never matches a colored target."""
    for y in range(renderer.cell_h * row, renderer.cell_h * (row + 1)):
        for x in range(round(renderer.cell_w * col), round(renderer.cell_w * (col + 1))):
            p = image.pixelColor(x, y)
            if (
                abs(p.red() - color.red()) <= tol
                and abs(p.green() - color.green()) <= tol
                and abs(p.blue() - color.blue()) <= tol
            ):
                return True
    return False


def test_blank_cell_is_default_background(renderer: TerminalRenderer, image: QImage) -> None:
    renderer.render(image, snapshot([make_row(blank_cell(), blank_cell())]))
    assert cell_pixel(image, renderer, 0) == DEFAULT_BG


def test_text_cell_paints_default_foreground(renderer: TerminalRenderer, image: QImage) -> None:
    renderer.render(image, snapshot([make_row(Cell("A"), blank_cell())]))
    assert cell_has_color(image, renderer, 0, DEFAULT_FG)


def test_palette_foreground(renderer: TerminalRenderer, image: QImage) -> None:
    renderer.render(image, snapshot([make_row(Cell("A", fg=1), blank_cell())]))
    assert cell_has_color(image, renderer, 0, QColor(0xCD, 0x00, 0x00))


def test_palette_background(renderer: TerminalRenderer, image: QImage) -> None:
    renderer.render(image, snapshot([make_row(Cell(" ", bg=2), blank_cell())]))
    assert cell_pixel(image, renderer, 0) == QColor(0x00, 0xCD, 0x00)


def test_rgb_color(renderer: TerminalRenderer, image: QImage) -> None:
    renderer.render(image, snapshot([make_row(Cell("A", fg=rgb(1, 2, 3)), blank_cell())]))
    assert cell_has_color(image, renderer, 0, QColor(1, 2, 3))


def test_bold_steps_ansi_to_bright(renderer: TerminalRenderer, image: QImage) -> None:
    renderer.render(image, snapshot([make_row(Cell("A", fg=1, bold=True), blank_cell())]))
    assert cell_has_color(image, renderer, 0, QColor(0xFF, 0x00, 0x00))
    assert not cell_has_color(image, renderer, 0, QColor(0xCD, 0x00, 0x00))


def test_cube_and_grayscale(renderer: TerminalRenderer) -> None:
    rows = [
        make_row(Cell("A", fg=22), blank_cell()),  # cube (0, 1, 0) → (0, 95, 0)
        make_row(Cell("A", fg=232), blank_cell()),  # grayscale: 8
    ]
    img = QImage(round(2 * renderer.cell_w), 2 * renderer.cell_h, QImage.Format.Format_RGB32)
    renderer.render(img, snapshot(rows))
    assert cell_has_color(img, renderer, 0, QColor(0, 95, 0), row=0)
    assert cell_has_color(img, renderer, 0, QColor(8, 8, 8), row=1)


def test_reverse_swaps_colors(renderer: TerminalRenderer, image: QImage) -> None:
    renderer.render(image, snapshot([make_row(Cell(" ", fg=1, reverse=True), blank_cell())]))
    assert cell_pixel(image, renderer, 0) == QColor(0xCD, 0x00, 0x00)  # fg became bg


def test_decsnm_inverts_the_whole_screen(renderer: TerminalRenderer, image: QImage) -> None:
    """DECSCNM (?5) inverts every cell: a plain cell shows the fg color
    as its background, and a cell with SGR reverse shows a normal bg
    (the two inversion layers XOR, effective_rendition)."""
    rev = Snapshot(
        dirty_rows=(0,),
        rows=(make_row(Cell(" "), Cell(" ", fg=1, reverse=True)),),
        scrollback_len=0,
        viewport_offset=0,
        cursor=(-1, 0),
        reverse_video=True,
    )
    renderer.render(image, rev)
    assert cell_pixel(image, renderer, 0) == DEFAULT_FG  # plain → inverted
    assert cell_pixel(image, renderer, 1) == QColor(0x10, 0x10, 0x10)  # reverse XOR ?5 → normal


def test_bold_bright_applies_after_inversion(renderer: TerminalRenderer, image: QImage) -> None:
    """Spec §7: inversion layers first, then bold-as-bright — a bold
    reverse cell shows the original *background* brightened as fg."""
    cells = make_row(Cell("A", bg=1, bold=True, reverse=True), blank_cell())
    renderer.render(image, snapshot([cells]))
    # Displayed fg = original bg 1, brightened by bold → bright red.
    assert cell_has_color(image, renderer, 0, QColor(0xFF, 0x00, 0x00))
    assert not cell_has_color(image, renderer, 0, QColor(0xCD, 0x00, 0x00))


def test_wide_char_draws_once_across_two_cells(renderer: TerminalRenderer) -> None:
    """A wide char advances two cells; the empty continuation cell gets
    its background but no glyph (screen.py's "" continuation)."""
    img = QImage(round(2 * renderer.cell_w), 1 * renderer.cell_h, QImage.Format.Format_RGB32)
    row = make_row(Cell("界", fg=1), Cell(""))  # lead + continuation
    renderer.render(img, snapshot([row]))
    # The glyph spans into the continuation cell: fg pixels in both
    # (antialiased — compared approximately).
    assert cell_has_color_approx(img, renderer, 0, QColor(0xCD, 0x00, 0x00))
    assert cell_has_color_approx(img, renderer, 1, QColor(0xCD, 0x00, 0x00))


def test_hidden_cell_paints_no_glyph(renderer: TerminalRenderer, image: QImage) -> None:
    renderer.render(image, snapshot([make_row(Cell("A", hidden=True), blank_cell())]))
    assert not cell_has_color(image, renderer, 0, DEFAULT_FG)


def test_incremental_snapshot_touches_only_dirty_rows(renderer: TerminalRenderer) -> None:
    before = Row([Cell("X")] + [blank_cell()] * 2)
    img = QImage(round(3 * renderer.cell_w), 2 * renderer.cell_h, QImage.Format.Format_RGB32)
    renderer.render(
        img,
        snapshot([before, make_row(blank_cell(), blank_cell(), blank_cell())]),
    )
    # Repaint only row 1: row 0 must still show "X".
    dirty = Snapshot(
        dirty_rows=(1,),
        rows=(make_row(Cell("Y"), blank_cell(), blank_cell()),),
        scrollback_len=0,
        viewport_offset=0,
        cursor=(-1, 0),
    )
    renderer.render(img, dirty)
    assert cell_has_color_approx(img, renderer, 0, DEFAULT_FG, row=0)  # the "X" survives
    assert cell_has_color_approx(img, renderer, 0, DEFAULT_FG, row=1)  # the "Y" landed


def test_cursor_is_reverse_block(renderer: TerminalRenderer, image: QImage) -> None:
    renderer.render(image, snapshot([make_row(blank_cell()), make_row(blank_cell())], cursor=(0, 0)))
    assert cell_pixel(image, renderer, 0) == DEFAULT_FG  # block over the default bg


def test_cursor_inverts_character_under_it(renderer: TerminalRenderer) -> None:
    # A character under the cursor renders inverted — the block is the
    # cell's foreground, the glyph its background — so the cursor never
    # hides the text it sits on (xterm). `rows` is the merged viewport
    # the widget passes (the snapshot alone is incremental).
    img = QImage(round(1 * renderer.cell_w), renderer.cell_h, QImage.Format.Format_RGB32)
    img.fill(Qt.GlobalColor.black)
    cell = Cell("X", fg=rgb(255, 0, 0), bg=rgb(0, 0, 255))
    renderer.render(img, snapshot([make_row(cell)], cursor=(0, 0)), rows=[make_row(cell)])
    assert cell_has_color(img, renderer, 0, QColor(255, 0, 0))  # the block
    assert cell_has_color(img, renderer, 0, QColor(0, 0, 255))  # the glyph


def test_cursor_inverts_default_character(renderer: TerminalRenderer) -> None:
    # A default-rendition character: block = default fg, glyph = default
    # bg — the character stays visible on the block.
    img = QImage(round(1 * renderer.cell_w), renderer.cell_h, QImage.Format.Format_RGB32)
    img.fill(Qt.GlobalColor.black)
    renderer.render(img, snapshot([make_row(Cell("M"))], cursor=(0, 0)), rows=[make_row(Cell("M"))])
    assert cell_has_color(img, renderer, 0, DEFAULT_FG)  # the block
    assert cell_has_color(img, renderer, 0, DEFAULT_BG)  # the glyph


def test_cursor_outline_leaves_character_visible(renderer: TerminalRenderer) -> None:
    # The unfocused cursor (CURSOR_OUTLINE): a hollow rectangle around
    # the cell — the character underneath stays visible, no block, no
    # inversion.
    img = QImage(round(1 * renderer.cell_w), renderer.cell_h, QImage.Format.Format_RGB32)
    img.fill(Qt.GlobalColor.black)
    cell = Cell("X", fg=rgb(255, 0, 0), bg=rgb(0, 0, 255))
    renderer.render(
        img,
        snapshot([make_row(cell)], cursor=(0, 0)),
        rows=[make_row(cell)],
        cursor_style="outline",
    )
    assert cell_has_color(img, renderer, 0, QColor(255, 0, 0))  # the glyph
    assert cell_has_color(img, renderer, 0, QColor(0, 0, 255))  # the background
    assert cell_has_color(img, renderer, 0, DEFAULT_FG)  # the outline
    assert cell_pixel(img, renderer, 0) != DEFAULT_FG  # no block at the center


def test_cursor_outline_on_blank_cell(renderer: TerminalRenderer, image: QImage) -> None:
    # An empty cell keeps the character-free rectangle: the outline is
    # drawn, the center stays the background.
    renderer.render(
        image,
        snapshot([make_row(blank_cell()), make_row(blank_cell())], cursor=(0, 0)),
        cursor_style="outline",
    )
    assert cell_has_color(image, renderer, 0, DEFAULT_FG)  # the outline
    assert cell_pixel(image, renderer, 0) == DEFAULT_BG  # center not filled


def test_cursor_block_fills_the_cell_corners(renderer: TerminalRenderer, image: QImage) -> None:
    # The focused block cursor fills the whole cell — the outline's
    # half-pixel inset must not leak into the block path (a thin
    # background-colored border around the block).
    renderer.render(
        image,
        snapshot([make_row(blank_cell()), make_row(blank_cell())], cursor=(0, 0)),
        cursor_style="block",
    )
    r = renderer
    for x, y in (
        (0, 0),
        (round(r.cell_w) - 1, 0),
        (0, r.cell_h - 1),
        (round(r.cell_w) - 1, r.cell_h - 1),
    ):
        assert image.pixelColor(x, y) == DEFAULT_FG


def test_cursor_outline_stays_inside_the_cell(renderer: TerminalRenderer) -> None:
    # The unfocused outline must not bleed into the row below — a
    # drawRect pen is centered on the rect boundary, so an uninset rect
    # leaves a ~0.5px line in the next row, which the row-only repaint
    # never clears (the lingering top/bottom line).
    r = renderer
    img = QImage(round(1 * r.cell_w), 2 * r.cell_h, QImage.Format.Format_RGB32)
    img.fill(Qt.GlobalColor.black)
    renderer.render(
        img,
        snapshot([make_row(blank_cell()), make_row(blank_cell())], cursor=(0, 0)),
        cursor_style="outline",
    )
    for y in range(r.cell_h, 2 * r.cell_h):
        for x in range(round(r.cell_w)):
            assert img.pixelColor(x, y) == DEFAULT_BG


def test_hidden_cursor_draws_nothing(renderer: TerminalRenderer, image: QImage) -> None:
    # DECTCEM ?25l (cursor_visible=False): the block must not paint —
    # the donut demo hides the cursor while animating.
    renderer.render(
        image,
        snapshot([make_row(blank_cell()), make_row(blank_cell())], cursor=(0, 0), cursor_visible=False),
    )
    assert cell_pixel(image, renderer, 0) == DEFAULT_BG  # no block over the default bg


def test_cursor_override_hides_visible_snapshot(renderer: TerminalRenderer, image: QImage) -> None:
    # The widget's blink phase (cursor_visible=False) hides the block
    # even though the snapshot's DECTCEM says visible.
    renderer.render(
        image,
        snapshot([make_row(blank_cell()), make_row(blank_cell())], cursor=(0, 0)),
        cursor_visible=False,
    )
    assert cell_pixel(image, renderer, 0) == DEFAULT_BG


def test_cursor_override_shows_visible_snapshot(renderer: TerminalRenderer, image: QImage) -> None:
    # The blink phase True keeps the block over a visible snapshot.
    renderer.render(
        image,
        snapshot([make_row(blank_cell()), make_row(blank_cell())], cursor=(0, 0)),
        cursor_visible=True,
    )
    assert cell_pixel(image, renderer, 0) == DEFAULT_FG


def test_cursor_override_never_shows_hidden_snapshot(renderer: TerminalRenderer, image: QImage) -> None:
    # DECTCEM always wins: the override is ANDed with the snapshot's
    # visibility, so a cursor the app hid (?25l) stays hidden even when
    # the blink phase is True.
    renderer.render(
        image,
        snapshot([make_row(blank_cell()), make_row(blank_cell())], cursor=(0, 0), cursor_visible=False),
        cursor_visible=True,
    )
    assert cell_pixel(image, renderer, 0) == DEFAULT_BG


def test_cursor_paints_at_grid_row_plus_offset(renderer: TerminalRenderer) -> None:
    # Scrolled 2 up: grid row 1 shows at viewport row 1 + 2 = 3.
    img = QImage(round(1 * renderer.cell_w), 5 * renderer.cell_h, QImage.Format.Format_RGB32)
    img.fill(Qt.GlobalColor.black)
    renderer.render(
        img, snapshot([make_row(blank_cell())] * 5, cursor=(1, 0), viewport_offset=2)
    )
    assert cell_pixel(img, renderer, 0, 3) == DEFAULT_FG  # the cursor block
    assert cell_pixel(img, renderer, 0, 1) == DEFAULT_BG  # grid row 1, unscrolled


def test_cursor_off_viewport_draws_nothing(renderer: TerminalRenderer) -> None:
    # Grid row 4 + offset 2 = viewport row 6, beyond the 5-row viewport.
    img = QImage(round(1 * renderer.cell_w), 5 * renderer.cell_h, QImage.Format.Format_RGB32)
    img.fill(Qt.GlobalColor.black)
    renderer.render(
        img, snapshot([make_row(blank_cell())] * 5, cursor=(4, 0), viewport_offset=2)
    )
    for row in range(5):
        assert cell_pixel(img, renderer, 0, row) == DEFAULT_BG


def test_cursor_gate_uses_viewport_row(renderer: TerminalRenderer) -> None:
    # row_indices are viewport rows: grid row 0 + offset 1 = viewport row 1.
    marker = QColor(Qt.GlobalColor.red)
    rows = [make_row(Cell("M")), make_row(blank_cell())]
    snap = snapshot(rows, cursor=(0, 0), viewport_offset=1)
    img = QImage(round(1 * renderer.cell_w), 2 * renderer.cell_h, QImage.Format.Format_RGB32)
    img.fill(marker)
    painter = QPainter(img)
    try:
        renderer.paint(painter, snap, 2, None, row_indices=[1])
    finally:
        painter.end()
    assert cell_pixel(img, renderer, 0, 0) == marker  # row 0 untouched
    assert cell_pixel(img, renderer, 0, 1) == DEFAULT_FG  # cursor block at row 1
    # Without row 1 in row_indices the cursor must not paint anywhere.
    img2 = QImage(round(1 * renderer.cell_w), 2 * renderer.cell_h, QImage.Format.Format_RGB32)
    img2.fill(marker)
    painter = QPainter(img2)
    try:
        renderer.paint(painter, snap, 2, None, row_indices=[0])
    finally:
        painter.end()
    assert cell_has_color(img2, renderer, 0, DEFAULT_FG, row=0)  # row 0 M ink
    assert cell_pixel(img2, renderer, 0, 1) == marker  # row 1 untouched


# -- font / anti-aliasing ---------------------------------------------

def test_default_font_keeps_smoothing() -> None:
    # Glyphs are font-smoothed; crispness comes from the grid geometry,
    # not jagged masks.
    strategy = TerminalRenderer().font.styleStrategy()
    assert not (strategy & QFont.StyleStrategy.NoAntialias)


def test_antialias_opt_out_disables_smoothing() -> None:
    strategy = TerminalRenderer(antialias=False).font.styleStrategy()
    assert strategy & QFont.StyleStrategy.NoAntialias


def test_caller_font_is_not_mutated() -> None:
    font = QFont("Menlo", 12)
    original = font.styleStrategy()
    TerminalRenderer(font)  # copies the font, never mutates the caller's
    assert font.styleStrategy() == original


def test_set_font_replaces_font_and_metrics() -> None:
    renderer = TerminalRenderer()
    old_w, old_h = renderer.cell_w, renderer.cell_h
    big = QFont("Menlo", 24)
    renderer.set_font(big)
    assert renderer.font.family() == "Menlo"
    assert renderer.font.pointSize() == 24
    assert renderer.cell_w >= old_w
    assert renderer.cell_h >= old_h


def test_set_palette_replaces_defaults() -> None:
    renderer = TerminalRenderer()
    assert renderer.default_fg == DEFAULT_FG
    assert renderer.default_bg == DEFAULT_BG
    fg = QColor(0x00, 0x00, 0x00)
    bg = QColor(0xff, 0xff, 0xff)
    renderer.set_palette(fg, bg)
    assert renderer.default_fg == fg
    assert renderer.default_bg == bg


def test_set_palette_repaints_blank_cell_with_new_bg(renderer: TerminalRenderer, image: QImage) -> None:
    renderer.set_palette(QColor(0x00, 0x00, 0x00), QColor(0x12, 0x34, 0x56))
    renderer.render(image, snapshot([make_row(blank_cell())]))
    assert cell_pixel(image, renderer, 0) == QColor(0x12, 0x34, 0x56)


# -- vector box-drawing and block characters (no font seams) -------------


def test_box_drawing_horizontal_line_touches_both_edges(renderer: TerminalRenderer) -> None:
    # ─ (U+2500): a full-width line at mid-height — the font version
    # leaves gaps at the cell edges; drawLine must not.
    img = QImage(round(1 * renderer.cell_w), 1 * renderer.cell_h, QImage.Format.Format_RGB32)
    renderer.render(img, snapshot([make_row(Cell("─", fg=1))]))
    cy = renderer.cell_h // 2
    for x in range(round(renderer.cell_w)):
        assert img.pixelColor(x, cy) == QColor(0xCD, 0x00, 0x00)
    assert img.pixelColor(0, 0) != QColor(0xCD, 0x00, 0x00)  # nothing above


def test_box_drawing_corner_is_open_on_the_unjoined_side(renderer: TerminalRenderer) -> None:
    # ┌ (U+250C): horizontal reaches the right edge, vertical the bottom;
    # the top and left edges stay open (the neighbor cells join there).
    img = QImage(round(1 * renderer.cell_w), 1 * renderer.cell_h, QImage.Format.Format_RGB32)
    renderer.render(img, snapshot([make_row(Cell("\u250c", fg=1))]))
    cx, cy = round(renderer.cell_w // 2), renderer.cell_h // 2
    fg = QColor(0xCD, 0x00, 0x00)
    assert img.pixelColor(round(renderer.cell_w) - 1, cy) == fg
    assert img.pixelColor(cx, renderer.cell_h - 1) == fg
    assert img.pixelColor(0, cy) != fg  # left open
    assert img.pixelColor(cx, 0) != fg  # top open


#: Every table glyph with the cell edges its strokes must reach. A wrong
#: segment (a diagonal, or a missing arm) shows up as a colored pixel
#: where the cell must stay open, or an open edge where an arm belongs.
_BOX_ARMS = {
    0x2500: ("L", "R"),  # ─
    0x2502: ("T", "B"),  # │
    0x250C: ("R", "B"),  # ┌
    0x2510: ("L", "B"),  # ┐
    0x2514: ("R", "T"),  # └
    0x2518: ("L", "T"),  # ┘
    0x251C: ("T", "B", "R"),  # ├ — vertical at center + right arm
    0x2524: ("T", "B", "L"),  # ┤ — vertical at center + left arm
    0x252C: ("L", "R", "B"),  # ┬
    0x2534: ("L", "R", "T"),  # ┴
    0x253C: ("L", "R", "T", "B"),  # ┼
}


def test_box_drawing_arms_are_orthogonal(renderer: TerminalRenderer) -> None:
    # └ ┘ ┴ used to paint a diagonal from the top-left corner to the
    # center (0x6D in the DEC graphics set) — arms must meet the cell
    # edges at right angles, and the open corner must stay empty.
    for cp, arms in _BOX_ARMS.items():
        img = QImage(round(1 * renderer.cell_w), 1 * renderer.cell_h, QImage.Format.Format_RGB32)
        renderer.render(img, snapshot([make_row(Cell(chr(cp), fg=1))]))
        fg = QColor(0xCD, 0x00, 0x00)
        cx, cy = round(renderer.cell_w // 2), renderer.cell_h // 2
        probes = {
            "T": (cx, 0),
            "B": (cx, renderer.cell_h - 1),
            "L": (0, cy),
            "R": (round(renderer.cell_w) - 1, cy),
        }
        for name, (x, y) in probes.items():
            if name in arms:
                assert img.pixelColor(x, y) == fg, f"{chr(cp)}: {name} arm missing"
            else:
                assert img.pixelColor(x, y) != fg, f"{chr(cp)}: stray {name} arm"
        # The top-left corner: a diagonal segment would paint it.
        assert img.pixelColor(0, 0) != fg, f"{chr(cp)}: diagonal in the corner"


def test_rounded_corners_reach_their_own_edges(renderer: TerminalRenderer) -> None:
    # ╭╮╯╰ (U+256D–2570): each corner's strokes must reach its own cell
    # edges — ╭ the top and left edges, ╮ top and right, ╯ left and
    # bottom, ╰ right and bottom. The pre-table code drew all four
    # rotated 180° (╭ reached bottom and right).
    corners = {
        0x256D: ("T", "L"),  # ╭
        0x256E: ("T", "R"),  # ╮
        0x256F: ("B", "L"),  # ╯
        0x2570: ("B", "R"),  # ╰
    }
    for cp, arms in corners.items():
        img = QImage(round(1 * renderer.cell_w), 1 * renderer.cell_h, QImage.Format.Format_RGB32)
        renderer.render(img, snapshot([make_row(Cell(chr(cp), fg=1))]))
        fg = QColor(0xCD, 0x00, 0x00)
        cx, cy = round(renderer.cell_w // 2), renderer.cell_h // 2
        probes = {
            "T": (cx, 0),
            "B": (cx, renderer.cell_h - 1),
            "L": (0, cy),
            "R": (round(renderer.cell_w) - 1, cy),
        }
        for name, (x, y) in probes.items():
            if name in arms:
                assert img.pixelColor(x, y) == fg, f"{chr(cp)}: {name} arm missing"
            else:
                assert img.pixelColor(x, y) != fg, f"{chr(cp)}: stray {name} arm"


def test_block_half_rows_join_seamlessly(renderer: TerminalRenderer) -> None:
    # ▀▀: two cells — the top halves must tile without a gap between
    # cells (the font version leaves seams at the boundaries).
    img = QImage(round(2 * renderer.cell_w), 1 * renderer.cell_h, QImage.Format.Format_RGB32)
    renderer.render(img, snapshot([make_row(Cell("▀"), Cell("▀"))]))
    for x in range(round(2 * renderer.cell_w)):
        assert img.pixelColor(x, 0) == DEFAULT_FG
    # The seam between the two cells is seamless.
    assert img.pixelColor(round(renderer.cell_w) - 1, 0) == DEFAULT_FG
    assert img.pixelColor(round(renderer.cell_w), 0) == DEFAULT_FG
    # The bottom of the cell is untouched.
    assert img.pixelColor(0, renderer.cell_h - 1) == DEFAULT_BG


def test_block_quadrant_char_fills_only_its_quadrant(renderer: TerminalRenderer) -> None:
    # ▘ (U+2598): only the top-left quadrant is lit.
    img = QImage(round(1 * renderer.cell_w), 1 * renderer.cell_h, QImage.Format.Format_RGB32)
    renderer.render(img, snapshot([make_row(Cell("\u2598"))]))
    assert img.pixelColor(0, 0) == DEFAULT_FG
    assert img.pixelColor(round(renderer.cell_w) - 1, 0) == DEFAULT_BG
    assert img.pixelColor(0, renderer.cell_h - 1) == DEFAULT_BG
    assert img.pixelColor(round(renderer.cell_w) - 1, renderer.cell_h - 1) == DEFAULT_BG


@pytest.mark.parametrize("glyph", ["\u2b1d", "\u25aa", "\u25a0", "\u25cf", "\u2022",
                                   "\u00b7", "\u25c6", "\u25b2", "\u25b6", "\u25fc",
                                   "\u2b24", "\u2b1b"])
def test_shape_glyphs_draw_centered_vector_fills(renderer: TerminalRenderer, glyph: str) -> None:
    # The geometric-shape family (⬝ ▪ ■ ● • · ◆ ▲ ▶ ◼ ⬤ ⬛): filled
    # vector shapes — centered in the cell, small enough that the
    # corners stay empty, and visible (the font glyphs for the small
    # ones wash out to a speck).
    img = QImage(round(1 * renderer.cell_w), 1 * renderer.cell_h, QImage.Format.Format_RGB32)
    renderer.render(img, snapshot([make_row(Cell(glyph, fg=1))]))
    fg = QColor(0xCD, 0x00, 0x00)
    painted = [
        (x, y)
        for y in range(renderer.cell_h)
        for x in range(round(renderer.cell_w))
        if img.pixelColor(x, y) == fg
    ]
    assert painted, f"{glyph}: the shape must paint"
    xs = [p[0] for p in painted]
    ys = [p[1] for p in painted]
    # Centered and inside the cell: corners stay empty.
    assert min(xs) > 0 and max(xs) < round(renderer.cell_w) - 1, f"{glyph}: touches an edge"
    assert min(ys) > 0 and max(ys) < renderer.cell_h - 1, f"{glyph}: touches an edge"
    # The vertical middle of the cell is painted (centered).
    assert any(y == renderer.cell_h // 2 for y in ys), f"{glyph}: not vertically centered"


@pytest.mark.parametrize("glyph", ["\u25a1", "\u25cb", "\u25c7", "\u25b3"])  # □ ○ ◇ △
def test_ring_shapes_draw_outlines_not_fills(renderer: TerminalRenderer, glyph: str) -> None:
    # Hollow shapes are outlines: the rim paints, the interior shows
    # the cell background through.
    img = QImage(round(1 * renderer.cell_w), 1 * renderer.cell_h, QImage.Format.Format_RGB32)
    renderer.render(img, snapshot([make_row(Cell(glyph, fg=1))]))
    fg = QColor(0xCD, 0x00, 0x00)
    painted = [
        (x, y)
        for y in range(renderer.cell_h)
        for x in range(round(renderer.cell_w))
        if img.pixelColor(x, y) == fg
    ]
    assert painted, f"{glyph}: the outline must paint"
    # The outline is a rim: the cell center stays unpainted for a ring
    # square and a ring circle (diamonds/triangles are filled outlines,
    # so this only holds for □ ○).
    if glyph in ("\u25a1", "\u25cb"):
        assert img.pixelColor(round(renderer.cell_w // 2), renderer.cell_h // 2) != fg


@pytest.mark.parametrize("glyph", ["\u2503", "\u2551", "\u2550", "\u2567"])  # ┃ ║ ═ ╧
def test_unlisted_box_variants_fall_back_to_the_font(renderer: TerminalRenderer, glyph: str) -> None:
    # Heavy/double/dashed box variants sit inside the dense 0x2500–257F
    # range the Cython path classifies in C, but they are not in
    # `_VECTOR_GLYPHS` — the vector drawer must fall back to the font
    # instead of raising (a real-world crash: opencode renders ┃).
    img = QImage(round(1 * renderer.cell_w), 1 * renderer.cell_h, QImage.Format.Format_RGB32)
    renderer.render(img, snapshot([make_row(Cell(glyph, fg=1))]))
    assert cell_has_color_approx(img, renderer, 0, QColor(0xCD, 0x00, 0x00)), (
        f"{glyph}: the font fallback must paint"
    )


def test_braille_stays_in_the_font(renderer: TerminalRenderer) -> None:
    # Braille (U+2800–28FF) is intentionally font-rendered: the font
    # glyphs carry the correct dot patterns (the six-dots-circling
    # spinner). It must NOT be classified as a vector glyph, and the
    # glyph must paint through the normal text path.
    from pyqtermx.render import _VECTOR_CODES
    assert not (_VECTOR_CODES & set(range(0x2800, 0x2900))), "braille must stay in the font"
    img = QImage(round(1 * renderer.cell_w), 1 * renderer.cell_h, QImage.Format.Format_RGB32)
    renderer.render(img, snapshot([make_row(Cell("\u280b", fg=1))]))  # ⠋
    assert cell_has_color_approx(img, renderer, 0, QColor(0xCD, 0x00, 0x00)), (
        "the font must paint the braille glyph"
    )


# -- dim / strike / overline / italic (the rest of the SGR set) ----------


def test_dim_mixes_foreground_toward_background(renderer: TerminalRenderer) -> None:
    img = QImage(round(1 * renderer.cell_w), 1 * renderer.cell_h, QImage.Format.Format_RGB32)
    renderer.render(img, snapshot([make_row(Cell("M", dim=True))]))
    # SGR 2: fg = (DEFAULT_FG + DEFAULT_BG) / 2 per channel. The exact
    # color lives in the glyph cores — AA blends only edge pixels.
    mixed = QColor(0x7C, 0x7C, 0x7C)
    assert cell_has_color(img, renderer, 0, mixed)


def test_strike_and_overline_draw_lines(renderer: TerminalRenderer) -> None:
    img = QImage(round(1 * renderer.cell_w), 1 * renderer.cell_h, QImage.Format.Format_RGB32)
    renderer.render(img, snapshot([make_row(Cell("M", strike=True, overline=True))]))
    assert img.pixelColor(0, renderer.cell_h // 2) == DEFAULT_FG  # strike
    assert img.pixelColor(0, 0) == DEFAULT_FG  # overline


def test_italic_and_bold_fonts_are_cached(renderer: TerminalRenderer) -> None:
    italic = renderer._font_for(False, True)
    bold = renderer._font_for(True, False)
    assert italic.italic() and not italic.bold()
    assert bold.bold() and not bold.italic()
    # The cache returns the same object — no per-cell QFont churn.
    assert renderer._font_for(True, False) is bold


# -- partial rendering: row_indices bounds the repaint -------------------


def test_paint_row_indices_limits_the_repaint(renderer: TerminalRenderer) -> None:
    # Two rows of M; row_indices=[1] must paint only row 1 — row 0
    # stays untouched (the pre-filled red), so a one-row update costs
    # one row of paint calls, not the whole frame.
    marker = QColor(Qt.GlobalColor.red)
    rows = [make_row(Cell("M")), make_row(Cell("M"))]
    img = QImage(round(1 * renderer.cell_w), 2 * renderer.cell_h, QImage.Format.Format_RGB32)
    img.fill(marker)
    painter = QPainter(img)
    try:
        renderer.paint(painter, snapshot(rows), 2, None, row_indices=[1])
    finally:
        painter.end()
    assert cell_pixel(img, renderer, 0, 0) == marker  # row 0 untouched
    # Row 1 painted: glyph ink present in the cell (edge pixels are
    # AA-blended; the stroke cores are exact).
    assert cell_has_color(img, renderer, 0, DEFAULT_FG, row=1)


def test_paint_row_indices_gate_the_cursor(renderer: TerminalRenderer) -> None:
    # The cursor paints only when its viewport row is in row_indices —
    # a repaint of row 0 must not draw the row-1 cursor (it would leak
    # outside the damaged region without clipping).
    marker = QColor(Qt.GlobalColor.red)
    rows = [make_row(Cell("M")), make_row(Cell("M"))]
    img = QImage(round(1 * renderer.cell_w), 2 * renderer.cell_h, QImage.Format.Format_RGB32)
    img.fill(marker)
    snap = snapshot(rows, cursor=(1, 0))
    painter = QPainter(img)
    try:
        renderer.paint(painter, snap, 2, None, row_indices=[0])
    finally:
        painter.end()
    # Row 0: painted — glyph ink present (its row is in row_indices).
    assert cell_has_color(img, renderer, 0, DEFAULT_FG)
    # Row 1: untouched — the cursor row was not in row_indices.
    assert cell_pixel(img, renderer, 0, 1) == marker


def test_render_row_indices_limit_the_widget_seam(renderer: TerminalRenderer) -> None:
    # The widget's seam: `render` must forward `row_indices`, so a
    # one-row snapshot re-rasterizes one row into the backing image,
    # not the whole frame (the delete-slowness regression: render()
    # dropped the parameter, so every keypress repainted all rows).
    marker = QColor(Qt.GlobalColor.red)
    rows = [make_row(Cell("M")), make_row(Cell("M"))]
    img = QImage(round(1 * renderer.cell_w), 2 * renderer.cell_h, QImage.Format.Format_RGB32)
    img.fill(marker)
    renderer.render(img, snapshot(rows), rows=rows, row_indices=[1])
    assert cell_pixel(img, renderer, 0, 0) == marker  # row 0 untouched
    assert cell_has_color(img, renderer, 0, DEFAULT_FG, row=1)  # row 1 painted


# -- Selection overlay ----------------------------------------------------


def test_selection_paints_reversed_background(renderer: TerminalRenderer, image: QImage) -> None:
    # Selected cells swap fg/bg (the classic terminal highlight): a
    # blank red-fg cell paints its background red while selected.
    rows = [make_row(*(Cell(" ", fg=rgb(255, 0, 0), bg=rgb(0, 0, 255)) for _ in range(2)))]
    renderer.render(image, snapshot(rows), selection=Selection(0, 0, 0, 0))
    assert cell_pixel(image, renderer, 0) == QColor(255, 0, 0)
    assert cell_pixel(image, renderer, 1) == QColor(0, 0, 255)


def test_selection_swaps_glyph_color(renderer: TerminalRenderer, image: QImage) -> None:
    # The glyph of a selected cell is painted in the cell's background
    # color (the swap applies to both passes).
    rows = [make_row(Cell("x", fg=rgb(255, 0, 0), bg=rgb(0, 0, 255)), blank_cell())]
    renderer.render(image, snapshot(rows), selection=Selection(0, 0, 0, 0))
    # the glyph is painted in the (swapped) background color — antialiased,
    # so compared approximately (the selected background below is a pure fill)
    assert cell_has_color_approx(image, renderer, 0, QColor(0, 0, 255))
    # ...and the selected background is the foreground color
    assert any(
        image.pixelColor(x, y) == QColor(255, 0, 0)
        for y in range(renderer.cell_h)
        for x in range(round(renderer.cell_w))
    )


def test_selection_range_bounds_painting(renderer: TerminalRenderer) -> None:
    # cols 1..2 of row 0 selected: the neighbors keep their own bg.
    img = QImage(round(3 * renderer.cell_w), renderer.cell_h, QImage.Format.Format_RGB32)
    img.fill(Qt.GlobalColor.black)
    colored = Cell(" ", fg=rgb(255, 0, 0), bg=rgb(0, 0, 255))
    rows = [make_row(colored, colored, colored)]
    renderer.render(img, snapshot(rows), selection=Selection(0, 1, 0, 2))
    assert cell_pixel(img, renderer, 0) == QColor(0, 0, 255)
    assert cell_pixel(img, renderer, 1) == QColor(255, 0, 0)
    assert cell_pixel(img, renderer, 2) == QColor(255, 0, 0)


def test_selection_multi_row_open_ends(renderer: TerminalRenderer) -> None:
    # Rows 0-1 selected: row 0 from col 1 to the end, row 1 fully.
    img = QImage(round(3 * renderer.cell_w), 2 * renderer.cell_h, QImage.Format.Format_RGB32)
    img.fill(Qt.GlobalColor.black)
    colored = Cell(" ", fg=rgb(255, 0, 0), bg=rgb(0, 0, 255))
    rows = [make_row(colored, colored, colored) for _ in range(2)]
    renderer.render(img, snapshot(rows), selection=Selection(0, 1, 1, 1))
    assert cell_pixel(img, renderer, 0, 0) == QColor(0, 0, 255)  # row 0, col 0: outside
    assert cell_pixel(img, renderer, 2, 0) == QColor(255, 0, 0)  # row 0, col 2: open end
    assert cell_pixel(img, renderer, 0, 1) == QColor(255, 0, 0)  # row 1 fully selected


def test_selection_rectangular_slices(renderer: TerminalRenderer) -> None:
    # Alt-drag rectangle: col 0 across rows 0-1 — col 1 is never
    # selected on either row.
    img = QImage(round(2 * renderer.cell_w), 2 * renderer.cell_h, QImage.Format.Format_RGB32)
    img.fill(Qt.GlobalColor.black)
    colored = Cell(" ", fg=rgb(255, 0, 0), bg=rgb(0, 0, 255))
    rows = [make_row(colored, colored) for _ in range(2)]
    renderer.render(img, snapshot(rows), selection=Selection(0, 0, 1, 0, rectangular=True))
    assert cell_pixel(img, renderer, 0, 0) == QColor(255, 0, 0)
    assert cell_pixel(img, renderer, 1, 0) == QColor(0, 0, 255)
    assert cell_pixel(img, renderer, 0, 1) == QColor(255, 0, 0)
    assert cell_pixel(img, renderer, 1, 1) == QColor(0, 0, 255)


def test_selection_does_not_shift_glyphs(renderer: TerminalRenderer) -> None:
    """A selection splits a glyph run at its boundary; the unselected
    cells must re-render pixel-identically to the selectionless frame.
    The old int cell_w made the split re-anchor at an integer boundary
    while drawText laid glyphs out at the font's fractional advance —
    the glyphs after the boundary shifted (the "text moves when
    selecting" bug). Float cell_w == the layout advance, so the split
    runs land exactly where the continuous run did."""
    n = 20
    row = make_row(*(Cell("M") for _ in range(n)))
    img_plain = QImage(round(n * renderer.cell_w), renderer.cell_h, QImage.Format.Format_RGB32)
    img_sel = QImage(round(n * renderer.cell_w), renderer.cell_h, QImage.Format.Format_RGB32)
    renderer.render(img_plain, snapshot([row]))
    renderer.render(img_sel, snapshot([row]), selection=Selection(0, 5, 0, 14))
    for col in list(range(0, 5)) + list(range(15, n)):
        for y in range(renderer.cell_h):
            for x in range(round(renderer.cell_w * col), round(renderer.cell_w * (col + 1))):
                assert img_sel.pixelColor(x, y) == img_plain.pixelColor(x, y), (
                    f"cell {col} shifted by the selection at pixel ({x}, {y})"
                )
