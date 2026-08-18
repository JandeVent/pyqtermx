# -*- coding: utf-8 -*-
# Copyright (C) 2018-2026 Connet Information Technology Company, Shanghai.
"""The screen model — the dumb grid the renderer reads.

The screen owns the display grid (cells), the cursor, and the scroll
primitives. It knows nothing about escape sequences; the emulator turns
parse events into calls on this model.

The grid is dense: every cell materialized, each row its own list, so
scrollback (Step 4) and resize reflow attach without rewriting the grid.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Iterator, overload

from wcwidth import wcwidth

#: Default screen size, VT102 canonical.
DEFAULT_LINES = 24
DEFAULT_COLUMNS = 80

#: Mode numbers, named after their DEC/ANSI numbers (glossary "Mode").
#: DEC-private modes (with `?` prefix) live in their own namespace.
IRM = 4    # insert mode
DECOM = 6  # origin mode
DECAWM = 7  # autowrap
DECTCEM = 25  # cursor visible (active = shown; terminals start shown)
NLM = 20   # newline mode

#: The DEC Special Graphics map — the line-drawing charset (glossary):
#: ASCII 0x60–0x7E → box-drawing and math glyphs, `0x5F` → no-break
#: space. What `man` and `ls` boxes are made of.
_DEC_GRAPHICS: dict[int, str] = {
    0x5F: "\u00A0",  # no-break space
    0x60: "\u25C6",  # ◆
    0x61: "\u2592",  # ▒
    0x62: "\u2409",  # ␉
    0x63: "\u240C",  # ␌
    0x64: "\u240D",  # ␍
    0x65: "\u240A",  # ␊
    0x66: "\u00B0",  # °
    0x67: "\u00B1",  # ±
    0x68: "\u2424",  # ␤
    0x69: "\u240B",  # ␋
    0x6A: "\u2518",  # ┘
    0x6B: "\u2510",  # ┐
    0x6C: "\u250C",  # ┌
    0x6D: "\u2514",  # └
    0x6E: "\u253C",  # ┼
    0x6F: "\u23BA",  # ⎺
    0x70: "\u23BB",  # ⎻
    0x71: "\u2500",  # ─
    0x72: "\u23BC",  # ⎼
    0x73: "\u23BD",  # ⎽
    0x74: "\u251C",  # ├
    0x75: "\u2524",  # ┤
    0x76: "\u2534",  # ┴
    0x77: "\u252C",  # ┬
    0x78: "\u2502",  # │
    0x79: "\u2264",  # ≤
    0x7A: "\u2265",  # ≥
    0x7B: "\u03C0",  # π
    0x7C: "\u2260",  # ≠
    0x7D: "\u00A3",  # £
    0x7E: "\u00B7",  # ·
}

#: The UK charset (`ESC ( A`): only `#` differs from ASCII.
_UK: dict[int, str] = {0x23: "\u00A3"}

#: Charset tables by designation final; "B" (ASCII) is the identity.
_CHARSETS: dict[str, dict[int, str]] = {"B": {}, "A": _UK, "0": _DEC_GRAPHICS}

#: Designation prefix → slot index: ESC ( → G0, ) → G1, * → G2, + → G3.
_DESIGNATORS = {"(": 0, ")": 1, "*": 2, "+": 3}

#: RGB colors (SGR 38;2 / 48;2) are ints with the high bit set —
#: `(r << 16) | (g << 8) | b | 0x1000000` — so they can never collide
#: with the -1 default or the 0–255 palette indices (ADR-0004).
_RGB_MARKER = 0x1000000


def rgb(r: int, g: int, b: int) -> int:
    """Encode an RGB color as a cell color int (ADR-0004)."""
    return (r << 16) | (g << 8) | b | _RGB_MARKER


def is_rgb(color: int) -> bool:
    """True for RGB cell colors (never true for -1 or a palette index)."""
    return color >= _RGB_MARKER


def rgb_parts(color: int) -> tuple[int, int, int]:
    """Decode an RGB cell color into (r, g, b)."""
    return ((color >> 16) & 0xFF, (color >> 8) & 0xFF, color & 0xFF)


#: Cap for the cell flyweight: enough for any plausible active grid
#: (2 screens × 24×80) plus scrollback churn; bounds the cache's
#: memory when a workload streams unique cells.
_CELL_INTERN_CAP = 4096


def _pack_rendition(fg: int, bg: int, bold: bool, underline: bool, reverse: bool,
                    blink: bool, dim: bool, italic: bool, hidden: bool,
                    strike: bool, overline: bool) -> int:
    """Pack cell rendition (fg, bg, 9 flags) into a single int for fast
    flyweight lookup (C3). The 12-field tuple key created one tuple + one
    hash per character; a pre-computed int key cuts hash to a single
    integer operation.

    Bit layout (61 bits total, fits in a Python int):
        bits  0–25  fg   (26 bits: covers -1→0, 0–255 palette, 0–0x1FFFFFF RGB)
        bits 26–51  bg   (26 bits)
        bits    52   bold
        bits    53   underline
        bits    54   reverse
        bits    55   blink
        bits    56   dim
        bits    57   italic
        bits    58   hidden
        bits    59   strike
        bits    60   overline
    """
    return (
        ((fg + 1) & 0x3FFFFFF)
        | ((bg + 1) & 0x3FFFFFF) << 26
        | (bold << 52)
        | (underline << 53)
        | (reverse << 54)
        | (blink << 55)
        | (dim << 56)
        | (italic << 57)
        | (hidden << 58)
        | (strike << 59)
        | (overline << 60)
    )


@dataclass(frozen=True, slots=True)
class Cell:
    """One glyph plus its graphic rendition.

    `fg`/`bg` are ints: -1 the default, 0–255 the 256-color palette,
    and >= 0x1000000 an RGB value (see :func:`rgb`). Cells are
    immutable so rows can be handed to a renderer or a test without
    any mutation hazard.
    """

    data: str = " "
    fg: int = -1
    bg: int = -1
    # VT102 Character Attributes, extended with the full SGR set
    bold: bool = False
    underline: bool = False
    reverse: bool = False
    blink: bool = False
    dim: bool = False
    italic: bool = False
    hidden: bool = False
    strike: bool = False
    overline: bool = False

    @classmethod
    def blank(cls) -> "Cell":
        return cls()


@dataclass
class Row:
    """One row of the grid: the cells plus the wrapped marker.

    `wrapped` marks a row whose content continues from the row above
    (xterm.js's `isWrapped`): set on the row a wrap lands on, cleared
    by an explicit line feed, by full-row erase, or by DECALN — not
    by cursor motion or row/column shifts (xterm.js insertCells and
    deleteCells leave isWrapped alone). The marker rides with the row
    through scroll and reflow, which consults it to re-join rows when
    widening.

    Cells are a plain mutable list; rows are handed to tests and the
    renderer read-only, but only the screen mutates them.
    """

    cells: list[Cell]
    wrapped: bool = False

    def __len__(self) -> int:
        return len(self.cells)

    def __iter__(self) -> Iterator[Cell]:
        return iter(self.cells)

    @overload
    def __getitem__(self, index: int) -> Cell: ...

    @overload
    def __getitem__(self, index: slice) -> Row: ...

    def __getitem__(self, index: int | slice) -> Cell | Row:
        return self.cells[index] if isinstance(index, int) else Row(self.cells[index])

    def __setitem__(self, index: int, cell: Cell) -> None:
        self.cells[index] = cell


@dataclass
class Cursor:
    """The printable position plus what printing stamps on cells.

    `pending_wrap` is the deferred-wrap flag: after a print lands in the
    last column, the cursor sits there until the next printable character
    (or any cursor motion) resolves it.
    """

    x: int = 0
    y: int = 0
    pending_wrap: bool = False
    fg: int = -1
    bg: int = -1
    # VT102 Character Attributes, extended with the full SGR set
    bold: bool = False
    underline: bool = False
    reverse: bool = False
    blink: bool = False
    dim: bool = False
    italic: bool = False
    hidden: bool = False
    strike: bool = False
    overline: bool = False


@dataclass(frozen=True)
class _SavedState:
    """The DECSC save slot: the cursor (position + full rendition), the
    four charset slots and active level, and the origin/wraparound modes.
    Tab stops and the scroll region are deliberately not saved (xterm.js
    saveCursor; spec line 81)."""

    cursor: Cursor
    charsets: tuple[str, str, str, str]
    charset_level: int
    decom: bool
    decawm: bool


@dataclass
class _ScreenState:
    """Everything that travels with a grid (ADR-0004): the rows, the
    cursor *position* (the rendition is shared — one copy on the
    screen), the scroll region, the tab stops, and the DECSC save slot.
    One state per grid: index 0 the normal screen, 1 the alternate.

    The normal state additionally owns the scrollback (ADR-0006): the
    retained history rows above the grid plus the viewport offset. The
    alternate screen has neither — its viewport is always at the
    bottom, and history is never written or read while it is active."""

    grid: list[Row]
    scroll_top: int = 0
    scroll_bottom: int = 0
    tab_stops: set[int] = field(default_factory=set)
    saved_state: "_SavedState | None" = None
    #: Retained history (ADR-0006): rows pushed off the top of the
    #: normal grid by full-screen scrolling, oldest first. Bounded by
    #: the screen's scrollback_limit; only ED3 erases it.
    scrollback: list[Row] = field(default_factory=list)
    #: The viewport offset — rows up from the bottom (0 = live output).
    scroll_offset: int = 0
    #: The cursor position snapshot. Write-only bookkeeping: the live
    #: position rides the shared cursor and is synced into the state on
    #: every switch, so these fields are never read for the carry —
    #: they exist to keep each grid's position explicit and are clamped
    #: by resize. Reserved for a future renderer that needs the
    #: inactive grid's cursor.
    x: int = 0
    y: int = 0
    pending_wrap: bool = False


@dataclass
class Screen:
    """The grid of cells plus the cursor — the dumb model.

    Printing is width-aware (wide chars take two cells), wrap is deferred
    (pending-wrap flag), and scrolling happens inside a scroll region
    (full-screen by default; DECSTBM narrows it in Step 4).

    Two grids live on the screen — normal and alternate (ADR-0004) —
    with an active pointer; the properties below delegate to the active
    one. Cursor position is per-grid, the rendition shared.
    """

    lines: int = DEFAULT_LINES
    columns: int = DEFAULT_COLUMNS
    #: Scrollback cap (ADR-0006): how many history rows the normal
    #: screen retains, oldest dropped first. 0 disables scrollback.
    scrollback_limit: int = 1000

    #: Active modes, one set per namespace (ANSI / DEC-private). DECAWM
    #: starts on — autowrap is the default (xterm.js wraparoundMode);
    #: DECTCEM too — the cursor starts visible (xterm.js cursorBlink
    #: default). Modes are shared across grids (ADR-0004).
    _ansi_modes: set[int] = field(default_factory=set)
    _dec_modes: set[int] = field(default_factory=lambda: {DECAWM, DECTCEM})
    #: The active cursor: per-grid position, shared rendition
    #: (ADR-0004) — switching grids syncs the position fields.
    cursor: Cursor = field(default_factory=Cursor)
    #: Charset slots G0–G3, each named by its designation final ("B"
    #: ASCII by default) — glossary "Charset designation". Shared.
    _charsets: list[str] = field(default_factory=lambda: ["B", "B", "B", "B"])
    #: The active slot: print translates ASCII through this slot's map.
    _charset_level: int = 0
    #: The two grids' states: [0] normal, [1] alternate (ADR-0004).
    _screens: list[_ScreenState] = field(default_factory=list, init=False)
    #: Which grid is active.
    _active: int = field(init=False)
    #: Rows whose content changed since the last take (ADR-0005): the
    #: snapshot transport's change list, cleared on read.
    _dirty_rows: set[int] = field(default_factory=set, init=False)
    #: The cell flyweight: cells are frozen, so identical (data,
    #: rendition) can share one object — terminal output repeats
    #: characters massively, and the paste/flood workloads build the
    #: same cell values over and over. Bounded: cleared when full.
    _cell_intern: dict[tuple[object, ...], Cell] = field(default_factory=dict, init=False)
    #: Cached erase-fill cell: invalidated when cursor.bg changes.
    _cached_erase_fill: Cell | None = field(default=None, init=False)
    _cached_erase_bg: int = field(default=None, init=False)  # type: ignore[assignment]

    # -- Per-screen delegation (ADR-0004) ------------------------------

    @property
    def _grid(self) -> list[Row]:
        """The active grid's rows."""
        return self._screens[self._active].grid

    @property
    def scroll_top(self) -> int:
        """The active screen's scroll region top."""
        return self._screens[self._active].scroll_top

    @scroll_top.setter
    def scroll_top(self, value: int) -> None:
        self._screens[self._active].scroll_top = value

    @property
    def scroll_bottom(self) -> int:
        """The active screen's scroll region bottom."""
        return self._screens[self._active].scroll_bottom

    @scroll_bottom.setter
    def scroll_bottom(self, value: int) -> None:
        self._screens[self._active].scroll_bottom = value

    @property
    def _tab_stops(self) -> set[int]:
        """The active screen's tab stops."""
        return self._screens[self._active].tab_stops

    @_tab_stops.setter
    def _tab_stops(self, value: set[int]) -> None:
        self._screens[self._active].tab_stops = value

    @property
    def _saved_state(self) -> "_SavedState | None":
        """The active screen's DECSC save slot."""
        return self._screens[self._active].saved_state

    @_saved_state.setter
    def _saved_state(self, value: "_SavedState | None") -> None:
        self._screens[self._active].saved_state = value

    def __post_init__(self) -> None:
        for _ in range(2):
            self._screens.append(
                _ScreenState(
                    grid=self._blank_rows(),
                    scroll_bottom=self.lines - 1,
                    tab_stops=set(range(0, self.columns, 8)),
                )
            )
        self._active = 0

    # -- Construction helpers -------------------------------------------

    def _blank_rows(self) -> list[Row]:
        return [self._blank_row() for _ in range(self.lines)]

    def _blank_row(self) -> Row:
        # One shared Cell for the whole row: cells are frozen and only
        # ever *replaced* (never mutated in place), so sharing is safe
        # and turns an 80-cell construction into one (the flood
        # workload scrolls this row in per line).
        return Row([Cell()] * self.columns)

    # -- Read API -------------------------------------------------------

    def line(self, y: int) -> Row:
        """The row at `y`: cells plus the wrapped marker."""
        return self._grid[y]

    def render(self) -> str:
        """The whole grid as text — one row per line, attrs stripped."""
        return "\n".join(
            "".join(cell.data for cell in row.cells) for row in self._grid
        )

    # -- Viewport (scrollback read API, ADR-0006) ------------------------

    @property
    def scrollback_len(self) -> int:
        """How many history rows the normal screen retains (always 0 on
        the alternate screen — it has no scrollback, ADR-0006)."""
        return 0 if self._active == 1 else len(self._screens[0].scrollback)

    @property
    def viewport_offset(self) -> int:
        """How many rows up from the bottom the viewport is scrolled
        (0 on the alternate screen, which has no history)."""
        return 0 if self._active == 1 else self._screens[0].scroll_offset

    @property
    def alt_screen(self) -> bool:
        """Whether the alternate screen is active (DECSET 47/1047/1049)
        — the widget's wheel policy: full-screen apps without mouse
        tracking get Up/Down arrows (line-by-line cursor moves) instead
        of a scrollback scroll, because the alternate screen has no
        history to scroll."""
        return self._active == 1

    def viewport_row(self, k: int) -> Row:
        """The k-th row of the visible viewport (top to bottom): with
        the scroll offset applied, history rows above the grid, then
        grid rows. On the alternate screen the viewport is the grid
        itself. The renderer's only scrollback read seam."""
        k = max(0, min(k, self.lines - 1))
        if self._active == 1:
            return self._grid[k]
        sb = self._screens[0].scrollback
        r = len(sb) - self._screens[0].scroll_offset + k
        if r < len(sb):
            return sb[r]
        return self._grid[r - len(sb)]

    def scroll(self, n: int) -> None:
        """Scroll the viewport by `n` rows (positive = up), clamped to
        the history. Model state — the renderer mirrors it from
        snapshots and posts commands; it never writes it directly
        (ADR-0005)."""
        state = self._screens[0]
        state.scroll_offset = max(0, min(len(state.scrollback), state.scroll_offset + n))

    def scroll_to_bottom(self) -> None:
        """Snap the viewport to the live output (offset 0)."""
        self._screens[0].scroll_offset = 0

    def clear_scrollback(self) -> None:
        """ED3 (`ESC[3J`): erase the retained history and snap the
        viewport to the bottom. The grid is untouched — only ED3
        erases history at runtime (ADR-0006)."""
        state = self._screens[0]
        state.scrollback.clear()
        state.scroll_offset = 0
        self._mark_all_dirty()

    # -- Change tracking (ADR-0005) ---------------------------------------

    def take_dirty_rows(self) -> set[int]:
        """The rows whose content changed since the last call — the
        snapshot transport's change list, consumed and cleared."""
        rows = self._dirty_rows
        self._dirty_rows = set()
        return rows

    def _mark_dirty(self, *ys: int) -> None:
        self._dirty_rows.update(ys)

    def _mark_all_dirty(self) -> None:
        self._dirty_rows.update(range(self.lines))

    # -- Printing -------------------------------------------------------

    def print(self, text: str) -> None:
        """Stamp `text` into the grid at the cursor, advancing it.

        Wrap is deferred: a pending wrap from a previous print resolves
        only when the next printable arrives (autowrap off — DECAWM —
        instead overwrites in place at the last column). Wide characters
        (wcwidth 2) fill their cell plus a blank continuation cell;
        combining marks (wcwidth 0) attach to the cell behind the
        cursor. Under insert mode (IRM), each character shifts the rest
        of the row right first.

        Hot path: modes, the charset table, and the active grid cannot
        change mid-batch (a print is one dispatch), so they are hoisted
        out of the loop; rows are marked dirty once per row touched.
        """
        if _print_text_fast is not None:
            _print_text_fast(self, text)
        else:
            self._print_slow(text)

    def _print_slow(self, text: str) -> None:
        """Pure-Python fallback for Screen.print() (kept for debugging)."""
        c = self.cursor
        grid = self._grid
        columns = self.columns
        decawm = self.mode(DECAWM, private=True)
        irm = self.mode(IRM)
        # The active charset's ASCII table — fixed for the whole batch
        # (a print is one dispatch; designation/shift events cannot
        # interleave). The default ASCII slot is identity — no table.
        translate = (
            None
            if self._charsets[self._charset_level] == "B"
            else _CHARSETS[self._charsets[self._charset_level]]
        )
        # The graphic rendition is the cursor's state and cannot change
        # mid-batch either (SGR is its own dispatch) — hoist it.
        fg = c.fg
        bg = c.bg
        bold = c.bold
        underline = c.underline
        reverse = c.reverse
        blink = c.blink
        dim = c.dim
        italic = c.italic
        hidden = c.hidden
        strike = c.strike
        overline = c.overline
        dirty = self._dirty_rows
        intern = self._cell_intern
        x = c.x
        y = c.y
        marked_y = -1
        cells_y = -1
        cells: list[Cell] = []
        # Pre-pack the rendition (C3): the 9 flags + fg/bg change only
        # between SGR dispatches — not per character. Computing the
        # packed int once per print() call (not per char) eliminates
        # 12-field tuple creation and tuple hashing from the hot loop.
        packed = _pack_rendition(fg, bg, bold, underline, reverse, blink,
                                 dim, italic, hidden, strike, overline)
        for char in text:
            cp = ord(char)
            if translate is not None and cp < 0x7F:
                char = translate.get(cp, char)
            if marked_y != y:
                dirty.add(y)
                marked_y = y
            if c.pending_wrap:
                wrapped = self._resolve_wrap()
                x = c.x
                y = c.y
                if wrapped:
                    self._mark_wrapped(y)
                # A wrap may have scrolled the grid — the row objects
                # changed, so the cached cells list is stale.
                cells_y = -1
            # ASCII has no wide or combining glyphs — skip the wcwidth
            # table entirely (the paste workload is ~100% ASCII).
            width = 1 if cp < 0x80 else wcwidth(char)
            if width < 0:
                continue  # control character — not printable
            if width == 0:
                # [A][◌̊] = [Å][ ]
                self._attach_combining(char)
                continue
            if width == 2 and x >= columns - 1:
                # Only one cell left. Autowrap on: the wide char
                # wraps to the next line first (xterm behavior);
                # off: it does not fit and is dropped (xterm.js).
                if decawm:
                    wrapped = self._resolve_wrap()
                    x = c.x
                    y = c.y
                    if wrapped:
                        self._mark_wrapped(y)
                    cells_y = -1  # a wrap may have scrolled the grid
                else:
                    continue
            if irm:
                # Insert mode: shift the row right by the char's
                # width, dropping trailing cells (xterm.js
                # insertCells + orphan cleanup).
                self._insert_cells(y, x, width)
            if cells_y != y:
                cells = grid[y].cells
                cells_y = y
            # The flyweight key mirrors Cell's fields in order — the
            # intern map turns a 12-field rendition into one shared
            # Cell instance (a row is a handful of distinct Cells).
            # C3: (char, packed_int) is a 2-element tuple — hashing a
            # string + int is much faster than hashing 12 fields.
            key = (char, packed)
            cell = intern.get(key)
            if cell is None:
                if len(intern) >= _CELL_INTERN_CAP:
                    intern.clear()
                cell = Cell(char, fg, bg, bold, underline, reverse, blink,
                            dim, italic, hidden, strike, overline)
                intern[key] = cell
            cells[x] = cell
            if marked_y != y:
                dirty.add(y)
                marked_y = y
            if width == 2:
                # Wide characters occupy two cells; only the lead
                # cell holds the glyph — the follow-up cell is an
                # empty continuation (renderers must skip it). The
                # continuation carries the full rendition like the
                # lead (xterm.js). Its key is the lead key with a
                # blank glyph — one field list, not two.
                cont_key = ("", packed)
                cont = intern.get(cont_key)
                if cont is None:
                    cont = Cell("", fg, bg, bold, underline, reverse, blink,
                                dim, italic, hidden, strike, overline)
                    intern[cont_key] = cont
                cells[x + 1] = cont
            x += width
            if x >= columns:
                x = columns - 1
                if decawm:
                    c.pending_wrap = True
            c.x = x

    def _insert_cells(self, y: int, x: int, n: int) -> None:
        """IRM/ICH: shift the row right by `n` cells at `x`, filling the
        gap with erase-fill cells and dropping cells past the edge
        (xterm.js BufferLine.insertCells). A wide lead split by the
        insertion point is blanked, as is a wide lead that lands on the
        last cell."""
        row = self._grid[y]
        if x and row[x - 1].data and wcwidth(row[x - 1].data[0]) == 2:
            # Inserting at the continuation cell of a wide char: the
            # split lead is blanked (xterm.js).
            row[x - 1] = self._erase_fill()
        if n < self.columns - x:
            for i in range(self.columns - x - n - 1, -1, -1):
                row[x + n + i] = row[x + i]
            for i in range(x, x + n):
                row[i] = self._erase_fill()
        else:
            for i in range(x, self.columns):
                row[i] = self._erase_fill()
        if row[self.columns - 1].data and wcwidth(row[self.columns - 1].data[0]) == 2:
            row[self.columns - 1] = self._erase_fill()

    def _erase_fill(self) -> Cell:
        """The blank cell IRM/ED/EL/ECH stamp: default foreground, the
        cursor's current background color, no attributes (xterm.js
        backColorErase default). Cached — bg changes are rare."""
        bg = self.cursor.bg
        if self._cached_erase_bg != bg:
            self._cached_erase_fill = Cell(" ", -1, bg)
            self._cached_erase_bg = bg
        return self._cached_erase_fill

    def _attach_combining(self, char: str) -> None:
        """Attach a combining mark to the cell behind the cursor; if that
        cell is a wide character's blank continuation, the glyph cell
        behind it instead. Dropped at column 0.
         [A][◌̊] = [Å][ ]
        """
        x = self.cursor.x
        if x == 0:
            return
        row = self._grid[self.cursor.y]
        if row[x - 1].data == "":
            x -= 1
        if x > 0:
            row[x - 1] = replace(row[x - 1], data=row[x - 1].data + char)

    def _mark_wrapped(self, y: int) -> None:
        """Mark the row at `y` as a wrapped row (xterm.js isWrapped)."""
        self._grid[y].wrapped = True

    def _wcwidth(self, char: str) -> int:
        """Wrapper for wcwidth — callable from Cython extensions."""
        return wcwidth(char)

    def _resolve_wrap(self) -> bool:
        """The next printable after a full line: move to the next line,
        scrolling the region if already at its bottom. Returns True when
        the cursor landed on a new row without scrolling — the caller
        marks that row wrapped (a wrap that scrolls lands on a fresh
        line and is not marked, matching xterm.js)."""
        self.cursor.pending_wrap = False
        self.cursor.x = 0
        if self.cursor.y == self.scroll_bottom:
            self._scroll_region(self.scroll_top, self.scroll_bottom, 1)
            return False
        if self.cursor.y < self.lines - 1:
            self.cursor.y += 1
            return True
        return False

    # -- Graphic rendition ----------------------------------------------

    def reset_rendition(self) -> None:
        """SGR 0: restore default fg/bg and clear every attribute flag."""
        c = self.cursor
        c.fg = -1
        c.bg = -1
        c.bold = c.dim = c.italic = c.underline = c.blink = False
        c.reverse = c.hidden = c.strike = c.overline = False

    def set_fg(self, color: int) -> None:
        """SGR 30–37/38;5/90–97/39: set the foreground palette index
        (-1 = default)."""
        self.cursor.fg = color

    def set_bg(self, color: int) -> None:
        """SGR 40–47/48;5/100–107/49: set the background palette index
        (-1 = default)."""
        self.cursor.bg = color

    def set_bold(self, on: bool = True) -> None:
        """SGR 1/22: bold on or off."""
        self.cursor.bold = on

    def set_dim(self, on: bool = True) -> None:
        """SGR 2/22: dim on or off."""
        self.cursor.dim = on

    def set_italic(self, on: bool = True) -> None:
        """SGR 3/23: italic on or off."""
        self.cursor.italic = on

    def set_underline(self, on: bool = True) -> None:
        """SGR 4/21/24: underline on or off (21 — double underline —
        collapses to the boolean)."""
        self.cursor.underline = on

    def set_blink(self, on: bool = True) -> None:
        """SGR 5/6/25: blink on or off (6 — rapid blink — collapses
        to blink)."""
        self.cursor.blink = on

    def set_reverse(self, on: bool = True) -> None:
        """SGR 7/27: reverse video on or off."""
        self.cursor.reverse = on

    def set_hidden(self, on: bool = True) -> None:
        """SGR 8/28: hidden on or off."""
        self.cursor.hidden = on

    def set_strike(self, on: bool = True) -> None:
        """SGR 9/29: strike-through on or off."""
        self.cursor.strike = on

    def set_overline(self, on: bool = True) -> None:
        """SGR 53/55: overline on or off."""
        self.cursor.overline = on

    # -- Modes ----------------------------------------------------------

    def mode(self, number: int, private: bool = False) -> bool:
        """Whether the mode is active. `private=True` reads the
        DEC-private namespace (`?`-prefixed), else the ANSI one."""
        modes = self._dec_modes if private else self._ansi_modes
        return number in modes

    def set_mode(self, number: int, private: bool = False) -> None:
        """SM/DECSET: activate a mode in the ANSI (default) or
        DEC-private namespace. Setting origin mode (DECOM) homes the
        cursor to the region top (xterm.js setMode 6)."""
        (self._dec_modes if private else self._ansi_modes).add(number)
        if private and number == DECOM:
            # Origin mode set: the cursor moves home — the top of the
            # scroll region (xterm.js setMode 6).
            self.cursor.pending_wrap = False
            self.cursor.x = 0
            self.cursor.y = self.scroll_top

    def reset_mode(self, number: int, private: bool = False) -> None:
        """RM/DECRST: deactivate a mode. Resetting origin mode (DECOM)
        homes the cursor to the screen top-left (xterm.js resetMode 6)."""
        (self._dec_modes if private else self._ansi_modes).discard(number)
        if private and number == DECOM:
            # Origin mode reset: the cursor moves home — screen top-left
            # (xterm.js resetMode 6).
            self.cursor.pending_wrap = False
            self.cursor.x = 0
            self.cursor.y = 0

    # -- Scroll region --------------------------------------------------

    def set_scroll_region(self, top: int, bottom: int) -> None:
        """DECSTBM: clamp both bounds, then ignore the region when its
        bottom is not below its top (xterm.js — nothing changes, no
        cursor move). On a valid set the cursor moves home: to
        (0, scroll_top) under origin mode, else (0, 0). Origin mode
        itself is not touched."""
        top = max(0, min(top, self.lines - 1))
        bottom = max(0, min(bottom, self.lines - 1))
        if bottom <= top:
            return
        self.scroll_top = top
        self.scroll_bottom = bottom
        self.cursor.pending_wrap = False
        self.cursor.x = 0
        self.cursor.y = self.scroll_top if self.mode(DECOM, private=True) else 0

    # -- Cursor motion --------------------------------------------------

    def carriage_return(self) -> None:
        """CR: move to column 0. Motion cancels any pending wrap."""
        self.cursor.pending_wrap = False
        self.cursor.x = 0

    def line_feed(self) -> None:
        """LF: move down one line; at the region bottom, scroll the
        region (a feed below the region moves down until the absolute
        bottom, where it is a no-op — xterm.js). Under newline mode
        (NLM), the feed also returns the cursor to column 0.

        An explicit line feed clears the wrapped marker on the line it
        lands on (xterm.js: only an explicit feed clears it, not CR)."""
        self.cursor.pending_wrap = False
        if self.mode(NLM):
            self.cursor.x = 0
        if self.cursor.y == self.scroll_bottom:
            self._scroll_region(self.scroll_top, self.scroll_bottom, 1)
        elif self.cursor.y != self.lines - 1:
            self.cursor.y += 1
            self._grid[self.cursor.y].wrapped = False

    def backspace(self) -> None:
        """BS: move left one column, clamped at 0."""
        self.cursor.pending_wrap = False
        self.cursor.x = max(0, self.cursor.x - 1)

    def tab(self) -> None:
        """HT: advance to the next tab stop (a CHT of 1)."""
        self.tab_forward(1)

    def set_tab_stop(self) -> None:
        """HTS: set a tab stop at the cursor column. Not cursor motion —
        the pending-wrap flag survives (xterm.js tabSet)."""
        self._tab_stops.add(self.cursor.x)

    def clear_tab_stop(self, mode: int) -> None:
        """TBC: 0 clears the stop at the cursor column, 3 clears all
        stops; other modes are unsupported and ignored (xterm.js)."""
        if mode == 0:
            self._tab_stops.discard(self.cursor.x)
        elif mode == 3:
            self._tab_stops.clear()

    def tab_forward(self, n: int = 1) -> None:
        """CHT: move forward `n` tab stops; past the last stop the
        cursor stays at the wrap position (real xterm xterm_next_tab
        returns `cols`)."""
        for _ in range(n):
            stop = self._next_stop(self.cursor.x)
            if stop >= self.columns:
                self.cursor.x = self.columns - 1
            else:
                self.cursor.pending_wrap = False
                self.cursor.x = stop

    def tab_backward(self, n: int = 1) -> None:
        """CBT: move backward `n` tab stops, clamped at 0 (xterm.js
        cursorBackwardTab)."""
        self.cursor.pending_wrap = False
        for _ in range(n):
            self.cursor.x = self._prev_stop(self.cursor.x)

    def _next_stop(self, x: int) -> int:
        """The first stop strictly after `x`, or `columns` — the wrap
        position — when there is none (real xterm xterm_next_tab)."""
        for i in range(x + 1, self.columns):
            if i in self._tab_stops:
                return i
        return self.columns

    def _prev_stop(self, x: int) -> int:
        """The first stop strictly before `x`, clamped to 0 (xterm.js
        Buffer.prevStop)."""
        for i in range(x - 1, -1, -1):
            if i in self._tab_stops:
                return i
        return 0

    def cursor_up(self, n: int = 1) -> None:
        """CUU: up n, clamped at the region top; above the region, free
        motion clamped at the screen top (xterm.js diffToTop)."""
        self.cursor.pending_wrap = False
        if self.cursor.y >= self.scroll_top:
            self.cursor.y = max(self.scroll_top, self.cursor.y - n)
        else:
            self.cursor.y = max(0, self.cursor.y - n)

    def cursor_down(self, n: int = 1) -> None:
        """CUD: down n, clamped at the region bottom; below the region,
        free motion clamped at the screen bottom (xterm.js diffToBottom)."""
        self.cursor.pending_wrap = False
        if self.cursor.y <= self.scroll_bottom:
            self.cursor.y = min(self.scroll_bottom, self.cursor.y + n)
        else:
            self.cursor.y = min(self.lines - 1, self.cursor.y + n)

    def cursor_forward(self, n: int = 1) -> None:
        """CUF: right n, clamped at the last column."""
        self.cursor.pending_wrap = False
        self.cursor.x = min(self.columns - 1, self.cursor.x + n)

    def cursor_backward(self, n: int = 1) -> None:
        """CUB: left n, clamped at 0."""
        self.cursor.pending_wrap = False
        self.cursor.x = max(0, self.cursor.x - n)

    def cursor_next_line(self, n: int = 1) -> None:
        """CNL: down n lines (clamped cursor motion — no scroll), then
        to column 0. (NEL is CR + index, which scrolls at the region
        bottom; the emulator composes it from CR and index.)"""
        self.cursor_down(n)
        self.cursor.x = 0

    def cursor_preceding_line(self, n: int = 1) -> None:
        """CPL: up n lines, then to column 0."""
        self.cursor_up(n)
        self.cursor.x = 0

    def set_cursor(self, x: int, y: int) -> None:
        """CUP/HVP: absolute position, 0-based here (the emulator
        converts from 1-based). Under origin mode, `y` is relative to
        the region top and the cursor is clamped to the region; else it
        is clamped to the screen."""
        self.cursor.pending_wrap = False
        if self.mode(DECOM, private=True):
            y = max(self.scroll_top, min(self.scroll_bottom, self.scroll_top + y))
        else:
            y = max(0, min(self.lines - 1, y))
        self.cursor.x = max(0, min(self.columns - 1, x))
        self.cursor.y = y

    def index(self) -> None:
        """IND: move down one line; at the region bottom, scroll the
        region (below the region, free motion to the absolute bottom).
        Unlike LF, an index does not clear the wrapped marker."""
        self.cursor.pending_wrap = False
        if self.cursor.y == self.scroll_bottom:
            self._scroll_region(self.scroll_top, self.scroll_bottom, 1)
        elif self.cursor.y != self.lines - 1:
            self.cursor.y += 1

    def reverse_index(self) -> None:
        """RI: move up one line; at the region top, scroll the region
        down — a fresh blank line at the top, the bottom row pushed out
        (xterm.js reverseIndex)."""
        self.cursor.pending_wrap = False
        if self.cursor.y == self.scroll_top:
            self._scroll_region_down(self.scroll_top, self.scroll_bottom, 1)
        else:
            self.cursor.y = max(0, self.cursor.y - 1)

    # -- Erase ----------------------------------------------------------

    def erase_in_display(self, mode: int = 0) -> None:
        """ED: erase with erase fill. 0: from the cursor down (rows
        below are reset to fresh blanks); 1: from the top to the cursor
        (rows above reset; the current row's wrapped marker is always
        cleared, and the next row's too when the whole row was erased);
        2: everything (every row reset). A row erased from column 0
        loses its wrapped marker (xterm.js eraseInDisplay)."""
        y = self.cursor.y
        x = self.cursor.x
        if mode == 0:
            row = self._grid[y]
            self._replace_cells(row, x, self.columns)
            if x == 0:
                row.wrapped = False
            self._mark_dirty(*range(y, self.lines))
            for yy in range(y + 1, self.lines):
                self._grid[yy] = self._erase_row()
        elif mode == 1:
            row = self._grid[y]
            self._replace_cells(row, 0, x + 1)
            row.wrapped = False
            if x + 1 >= self.columns and y + 1 < self.lines:
                self._grid[y + 1].wrapped = False
            self._mark_dirty(*range(0, y + 1))
            for yy in range(0, y):
                self._grid[yy] = self._erase_row()
        elif mode == 2:
            self._mark_all_dirty()
            for yy in range(self.lines):
                self._grid[yy] = self._erase_row()

    def erase_in_line(self, mode: int = 0) -> None:
        """EL: erase on the current row with erase fill. 0: from the
        cursor to the row end; 1: from the row start to the cursor
        (inclusive); 2: the whole row. A full-row erase (0 from column
        0, or 2) clears the wrapped marker; EL 1 never does (xterm.js
        _eraseInBufferLine clearWrap)."""
        row = self._grid[self.cursor.y]
        self._mark_dirty(self.cursor.y)
        if mode == 0:
            self._replace_cells(row, self.cursor.x, self.columns)
            if self.cursor.x == 0:
                row.wrapped = False
        elif mode == 1:
            self._replace_cells(row, 0, self.cursor.x + 1)
        elif mode == 2:
            self._replace_cells(row, 0, self.columns)
            row.wrapped = False

    def erase_chars(self, n: int = 1) -> None:
        """ECH: erase `n` cells from the cursor rightward, clamped to
        the row end. The wrapped marker survives (xterm.js eraseChars)."""
        row = self._grid[self.cursor.y]
        self._mark_dirty(self.cursor.y)
        count = min(n, self.columns - self.cursor.x)
        self._replace_cells(row, self.cursor.x, self.cursor.x + count)

    def _replace_cells(self, row: Row, start: int, end: int) -> None:
        """Erase cells [start, end) with erase fill, cleaning wide-char
        edges (xterm.js BufferLine.replaceCells): a lead split by the
        start is blanked, and a continuation stub whose lead is erased
        is blanked."""
        if start and row[start - 1].data and wcwidth(row[start - 1].data[0]) == 2:
            row[start - 1] = self._erase_fill()
        if end < self.columns and row[end - 1].data and wcwidth(row[end - 1].data[0]) == 2:
            row[end] = self._erase_fill()
        for i in range(start, end):
            row[i] = self._erase_fill()

    def _erase_row(self) -> Row:
        """A blank row stamped with the erase fill — the fill xterm.js
        uses for lines scrolled in by LF/IND/RI/SU/IL/DL and for rows
        reset by ED (cursor's bg, default fg). The fill is one shared
        Cell (frozen — sharing is safe); the cursor's bg cannot change
        mid-row."""
        return Row([self._erase_fill()] * self.columns)

    # -- Charsets -------------------------------------------------------

    def designate_charset(self, designator: str, charset: str) -> None:
        """`ESC ( C` etc: fill the slot named by `designator` (G0–G3)
        with the charset named by `charset`. Unknown charset names are
        parse-and-ignore — the slot keeps its previous set (xterm)."""
        slot = _DESIGNATORS.get(designator)
        if slot is not None and charset in _CHARSETS:
            self._charsets[slot] = charset

    def shift_charset(self, level: int) -> None:
        """SI/SO, `ESC n`/`o`, `ESC ~`/`}`/`|`: make the slot at `level`
        the active one, so print translates through its map."""
        self._charset_level = level

    # -- Save / restore -------------------------------------------------

    def save_state(self) -> None:
        """DECSC / CSI s: record the cursor (position + rendition), the
        charset slots and active level, and the origin/wraparound modes
        into the single save slot. The cursor is not touched."""
        self._saved_state = _SavedState(
            cursor=replace(self.cursor),
            charsets=(
                self._charsets[0],
                self._charsets[1],
                self._charsets[2],
                self._charsets[3],
            ),
            charset_level=self._charset_level,
            decom=self.mode(DECOM, private=True),
            decawm=self.mode(DECAWM, private=True),
        )

    def restore_state(self) -> None:
        """DECRC / CSI u: restore the saved state — modes first (their
        set/reset home the cursor, which the position restore then
        overrides), then the cursor clamped into the region under origin
        mode, else the screen. A restore before any save is a no-op."""
        saved = self._saved_state
        if saved is None:
            return
        if saved.decom:
            self.set_mode(DECOM, private=True)
        else:
            self.reset_mode(DECOM, private=True)
        if saved.decawm:
            self.set_mode(DECAWM, private=True)
        else:
            self.reset_mode(DECAWM, private=True)
        self.cursor = replace(saved.cursor)
        self.cursor.pending_wrap = False
        if self.mode(DECOM, private=True):
            y = max(self.scroll_top, min(self.scroll_bottom, self.cursor.y))
        else:
            y = max(0, min(self.lines - 1, self.cursor.y))
        self.cursor.y = y
        self.cursor.x = max(0, min(self.columns - 1, self.cursor.x))
        self._charsets = list(saved.charsets)
        self._charset_level = saved.charset_level

    # -- Alternate screen (ADR-0004) ------------------------------------

    def effective_rendition(self, x: int, y: int) -> tuple[int, int]:
        """The (fg, bg) a renderer should draw for the cell at (x, y):
        the cell's own colors with reverse video applied. Two reverse
        sources stack by XOR — the SGR `reverse` attribute and the
        DECSCNM mode (`?5`) — so both on cancels out, either alone
        swaps fg/bg. `render()` itself stays text-only; this is the
        single seam Phase 3 adds (the mode rides the generic DEC
        registry)."""
        cell = self._grid[y].cells[x]
        if cell.reverse != (5 in self._dec_modes):
            return (cell.bg, cell.fg)
        return (cell.fg, cell.bg)

    def decaln(self) -> None:
        """DECALN (`ESC # 8`, the screen alignment test): every row of
        the active grid becomes `E` in the cursor's *full current
        rendition* — foreground, background and SGR attributes — not
        the erase fill (which would use the default foreground). The
        wrapped markers are cleared, and the cursor is homed before and
        after; tab stops, the scroll region, the DECSC slot, the modes
        and the scrollback are untouched (ADR-0006)."""
        self.cursor.x = 0
        self.cursor.y = 0
        self.cursor.pending_wrap = False
        fill = Cell(
            "E",
            fg=self.cursor.fg,
            bg=self.cursor.bg,
            bold=self.cursor.bold,
            underline=self.cursor.underline,
            reverse=self.cursor.reverse,
            blink=self.cursor.blink,
            dim=self.cursor.dim,
            italic=self.cursor.italic,
            hidden=self.cursor.hidden,
            strike=self.cursor.strike,
            overline=self.cursor.overline,
        )
        self._grid[:] = [
            Row([replace(fill) for _ in range(self.columns)]) for _ in range(self.lines)
        ]
        self._mark_all_dirty()
        self.cursor.x = 0
        self.cursor.y = 0
        self.cursor.pending_wrap = False

    def enter_alt_screen(self) -> None:
        """DECSET 47/1047/1049: switch to the alternate grid. The alt
        grid is filled with the erase fill and the cursor position is
        carried over (xterm.js activateAltBuffer); the rendition is
        shared, so it is not saved here — `?1049` wraps this with
        save_state/restore_state in the emulator. Re-entering while
        already on the alternate screen is a no-op (xterm.js
        activateAltBuffer early-returns), so the alt content survives
        a redundant/nested DECSET."""
        if self._active == 1:
            return
        state = self._screens[1]
        state.grid = [self._erase_row() for _ in range(self.lines)]
        state.x = self.cursor.x
        state.y = self.cursor.y
        state.pending_wrap = self.cursor.pending_wrap
        self._active = 1
        self._mark_all_dirty()

    def leave_alt_screen(self) -> None:
        """DECRST 47/1047/1049: switch back to the normal grid. The
        alt's live cursor position carries back (xterm.js
        activateNormalBuffer writes it into the normal buffer — the
        snapshot kept in the state is updated first), then the alt grid
        is cleared. A leave while already on the normal screen is a
        no-op."""
        if self._active == 0:
            return
        state = self._screens[1]
        state.x = self.cursor.x
        state.y = self.cursor.y
        state.pending_wrap = self.cursor.pending_wrap
        # Symmetric with the entry fill: the cleared grid uses the
        # erase fill, like xterm.js fillViewportRows on deactivation.
        state.grid = [self._erase_row() for _ in range(self.lines)]
        self._active = 0
        self._mark_all_dirty()

    # -- Resize ---------------------------------------------------------

    def resize(self, lines: int, columns: int) -> None:
        """Resize both grids, re-wrapping every line at the new width
        (ADR-0003: reflow, not clip) — the normal and the alternate
        screen reflow independently (ADR-0004); the inactive one
        resizes invisibly. The normal screen reflows its history and
        grid as **one stream** (ADR-0006), so a wrapped line spanning
        the boundary re-joins exactly as if the screen were taller; the
        newest `lines` rows stay the grid, the rest remain history.
        Shrinking the height keeps each grid's bottom lines — the
        newest rows, blank or not (old text reflows into history, so a
        mostly-blank screen stays blank); the cursor and the viewport
        offset clamp in.
        Wrapped rows re-join the row above on widen (the marker rides
        the reflow)."""
        if lines < 1 or columns < 1:
            raise ValueError(f"resize: lines={lines} columns={columns} must be >= 1")
        self.lines = lines
        self.columns = columns
        for index, state in enumerate(self._screens):
            if index == 0:
                # One-stream reflow (ADR-0006): history + grid re-wrap
                # together; the newest `lines` rows become the grid.
                reflowed = self._reflow_rows(state.scrollback + state.grid, columns)
                if lines >= len(state.grid):
                    # Growing or steady height: the stream's trailing
                    # blank separators are padding the grid re-pads
                    # anyway — drop them so the newest `lines` rows are
                    # the content (a full grid re-wrapped to fewer rows
                    # must not lose its head to history).
                    while reflowed and not reflowed[-1].cells:
                        reflowed.pop()
                kept = reflowed[-lines:] if len(reflowed) >= lines else reflowed
                state.scrollback = reflowed[:-lines] if len(reflowed) > lines else []
                # The history's trailing blank separators are padding
                # (the grid's bottom blanks stayed in `kept` — they are
                # the newest rows); drop them so the scrollbar range
                # ends at the last content row.
                while state.scrollback and not state.scrollback[-1].cells:
                    state.scrollback.pop()
                state.scroll_offset = min(state.scroll_offset, len(state.scrollback))
            else:
                reflowed = self._reflow_rows(state.grid, columns)
                if lines >= len(state.grid):
                    while reflowed and not reflowed[-1].cells:
                        reflowed.pop()
                kept = reflowed[-lines:] if len(reflowed) >= lines else reflowed
            state.grid = []
            for row in kept:
                row.cells.extend([Cell.blank()] * (columns - len(row.cells)))
                state.grid.append(row)
            while len(state.grid) < lines:
                state.grid.append(self._blank_row())
            # Scroll region resets to full screen; tab stops to every-8
            # (xterm.js setupTabStops); saved positions clamp (xterm.js
            # Buffer.resize).
            state.scroll_top = 0
            state.scroll_bottom = lines - 1
            state.tab_stops = set(range(0, columns, 8))
            state.x = min(state.x, columns - 1)
            state.y = min(state.y, lines - 1)
            state.pending_wrap = False
            if state.saved_state is not None:
                saved = state.saved_state
                state.saved_state = replace(
                    saved,
                    cursor=replace(
                        saved.cursor,
                        x=min(saved.cursor.x, columns - 1),
                        y=min(saved.cursor.y, lines - 1),
                    ),
                )
        self.cursor.y = min(self.cursor.y, self.lines - 1)
        self.cursor.x = min(self.cursor.x, self.columns - 1)
        self.cursor.pending_wrap = False
        self._mark_all_dirty()

    @staticmethod
    def _reflow_rows(rows: list[Row], new_columns: int) -> list[Row]:
        """Re-wrap the text of `rows` at `new_columns`, preserving the
        graphic rendition of each glyph. A row marked wrapped continues
        the row above; an unwrapped row starts a new line — so distinct
        full-width rows no longer merge on widen (ADR-0003). Within a
        logical line, every re-wrapped segment after the first is marked
        wrapped, so narrow → widen round-trips re-join the same line.

        Only each row's *trailing* padding is dropped (it re-pads at the
        new width): leading and interior blanks are kept, and a fully
        blank row stays as a blank separator once content has been
        emitted. Wide characters fill two cells; a glyph that no longer
        fits at the row's end moves to the next. Trailing blank rows are
        kept — the caller (`resize`) splits the stream into history and
        grid by the newest `lines` rows, and the grid's bottom blanks
        are exactly those newest rows; trimming them here would let old
        content fall into the grid. The caller trims the history tail.
        """
        out: list[Row] = []
        out_row: list[Cell] = []
        x = 0
        cont = False  # the current out_row continues the previous output row
        pending_cont = False  # the next out_row to start will continue
        emitted = False
        blank = Cell.blank()
        for row in rows:
            if all(cell == blank for cell in row.cells):
                if emitted:
                    # A blank line between content: flush and keep it as a
                    # separator (padded by the caller).
                    if out_row:
                        out.append(Row(out_row, cont))
                        out_row = []
                        x = 0
                    out.append(Row([]))
                    pending_cont = False
                continue
            emitted = True
            if not row.wrapped and out_row:
                # A new line, not a continuation: flush what we have.
                out.append(Row(out_row, cont))
                out_row = []
                x = 0
                pending_cont = False
            if row.wrapped and not out_row:
                # A continuation row with nothing pending yet: it joins
                # the previous output row when the next cell lands.
                pending_cont = True
            # Trim only the trailing padding: find the last non-blank cell.
            last = max(i for i, cell in enumerate(row.cells) if cell != blank)
            for cell in row.cells[: last + 1]:
                if cell.data == "":
                    continue  # blank continuation of a wide char
                width = wcwidth(cell.data[0])
                if width == 0:
                    # Combining mark: attach to the previous glyph.
                    if out_row:
                        out_row[-1] = replace(out_row[-1], data=out_row[-1].data + cell.data)
                    continue
                if width == 2 and x >= new_columns - 1:
                    # A wide glyph that no longer fits at the row's end
                    # moves to the next line, which it continues.
                    out.append(Row(out_row, cont))
                    out_row = []
                    x = 0
                    pending_cont = True
                if not out_row:
                    cont = pending_cont
                    pending_cont = False
                out_row.append(cell)
                x += width
                if width == 2:
                    out_row.append(replace(Cell.blank(), data=""))
                if x >= new_columns:
                    out.append(Row(out_row, cont))
                    out_row = []
                    x = 0
                    pending_cont = True
        if out_row:
            out.append(Row(out_row, cont))
        return out

    # -- Scrolling ------------------------------------------------------

    def scroll_up(self, n: int = 1) -> None:
        """SU: scroll the region up by `n` lines; the top `n` lines are
        discarded, `n` erase-fill lines appear at the bottom. The cursor
        is not touched (xterm.js scrollUp)."""
        self._scroll_region(self.scroll_top, self.scroll_bottom, n)

    def scroll_down(self, n: int = 1) -> None:
        """SD: scroll the region down by `n` lines; the bottom `n` lines
        are discarded, `n` default-attr blank lines appear at the top.
        SD alone fills with default attributes, not the erase fill
        (xterm.js scrollDown). The cursor is not touched."""
        self._scroll_region_down(
            self.scroll_top, self.scroll_bottom, n, fill=self._blank_row()
        )

    # -- Row ops --------------------------------------------------------

    def insert_lines(self, n: int = 1) -> None:
        """IL: insert `n` blank lines at the cursor within the scroll
        region; rows below shift down, the region's bottom `n` rows are
        pushed out. No effect outside the region (xterm.js insertLines).
        The cursor returns to column 0; inserted lines are never
        wrapped."""
        if self.cursor.y > self.scroll_bottom or self.cursor.y < self.scroll_top:
            return
        self.cursor.pending_wrap = False
        self.cursor.x = 0
        self._mark_dirty(*range(self.scroll_top, self.scroll_bottom + 1))
        for _ in range(n):
            self._grid.pop(self.scroll_bottom)
            self._grid.insert(self.cursor.y, self._erase_row())

    def delete_lines(self, n: int = 1) -> None:
        """DL: delete `n` lines at the cursor within the scroll region;
        rows below shift up, `n` erase-fill lines appear at the region
        bottom. No effect outside the region (xterm.js deleteLines).
        The cursor returns to column 0."""
        if self.cursor.y > self.scroll_bottom or self.cursor.y < self.scroll_top:
            return
        self.cursor.pending_wrap = False
        self.cursor.x = 0
        self._mark_dirty(*range(self.scroll_top, self.scroll_bottom + 1))
        for _ in range(n):
            self._grid.pop(self.cursor.y)
            self._grid.insert(self.scroll_bottom, self._erase_row())

    def insert_chars(self, n: int = 1) -> None:
        """ICH: insert `n` erase-fill cells at the cursor, shifting the
        rest of the row right; cells past the edge are dropped. The
        cursor does not move (xterm.js insertChars)."""
        self._mark_dirty(self.cursor.y)
        self._insert_cells(self.cursor.y, self.cursor.x, n)

    def delete_chars(self, n: int = 1) -> None:
        """DCH: delete `n` cells at the cursor, shifting the rest of the
        row left; `n` erase-fill cells appear at the row end (xterm.js
        BufferLine.deleteCells). A wide lead split by the deletion is
        blanked, and a continuation cell left without its lead is
        blanked."""
        self._mark_dirty(self.cursor.y)
        self._delete_cells(self.cursor.y, self.cursor.x, n)

    def _delete_cells(self, y: int, x: int, n: int) -> None:
        row = self._grid[y]
        if n < self.columns - x:
            for i in range(self.columns - x - n):
                row[x + i] = row[x + n + i]
            for i in range(self.columns - n, self.columns):
                row[i] = self._erase_fill()
        else:
            for i in range(x, self.columns):
                row[i] = self._erase_fill()
        if x and row[x - 1].data and wcwidth(row[x - 1].data[0]) == 2:
            # The cell before the deletion point was a wide lead split
            # by the shift: blank it (xterm.js).
            row[x - 1] = self._erase_fill()
        if row[x].data == "":
            # A continuation cell whose lead was shifted or erased
            # (xterm.js: width 0 without content).
            row[x] = self._erase_fill()

    def _scroll_region(self, top: int, bottom: int, n: int, fill: Row | None = None) -> None:
        """Scroll the region [top, bottom] up by `n` lines; the top `n`
        lines leave the grid, `n` erase-fill lines appear at the bottom.

        A full-screen scroll on the normal screen pushes the leaving
        rows into the scrollback (ADR-0006); a narrowed region discards
        them, and the alternate screen never writes history."""
        full_screen = top == 0 and bottom == self.lines - 1
        self._shift_region(
            top, bottom, n, fill,
            up=True,
            scrollback=full_screen and self._active == 0,
        )

    def _shift_region(
        self,
        top: int,
        bottom: int,
        n: int,
        fill: Row | None = None,
        *,
        up: bool,
        scrollback: bool = False,
    ) -> None:
        """Shift the region [top, bottom] by `n` lines (up or down): the
        leaving lines are discarded — or pushed to scrollback when `up`
        with `scrollback` — and `n` erase-fill lines appear at the other
        edge. The whole region marks dirty.

        Each line is one slice shift, a C-level list move, instead of a
        Python loop per row (the flood workload scrolls 30k times).
        """
        grid = self._grid
        for _ in range(n):
            if up:
                if scrollback:
                    self._push_scrollback(grid[top])
                grid[top:bottom] = grid[top + 1 : bottom + 1]
                grid[bottom] = fill if fill is not None else self._erase_row()
            else:
                grid[top + 1 : bottom + 1] = grid[top:bottom]
                grid[top] = fill if fill is not None else self._erase_row()
        self._mark_dirty(*range(top, bottom + 1))

    def _push_scrollback(self, row: Row) -> None:
        """A row leaving the top of the normal grid enters history,
        bounded by the cap — oldest dropped first (ADR-0006)."""
        if self.scrollback_limit == 0:
            return
        sb = self._screens[0].scrollback
        sb.append(row)
        if len(sb) > self.scrollback_limit:
            del sb[: len(sb) - self.scrollback_limit]

    def _scroll_region_down(self, top: int, bottom: int, n: int, fill: Row | None = None) -> None:
        """Scroll the region [top, bottom] down by `n` lines (reverse
        index); the bottom `n` lines are discarded, `n` erase-fill lines
        appear at the top. Reverse scrolls never read or write the
        scrollback (ADR-0006 — the spec's retention contract)."""
        self._shift_region(top, bottom, n, fill, up=False)


# Late import to avoid circular dependency (_screen_fast imports screen).
try:
    from ._screen_fast import print_text as _print_text_fast
except ImportError:
    _print_text_fast = None
