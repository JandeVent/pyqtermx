# -*- coding: utf-8 -*-
# Copyright (C) 2018-2026 Connet Information Technology Company, Shanghai.
"""The emulator — turns parse events into screen operations.

Implements the dispatcher protocol (pyqtermx.dispatcher.Dispatcher), the
seam the parser already defines. The screen is the dumb model; the
emulator decides what each event means.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from .dispatcher import Dispatcher
from .palette import DEFAULT_BG_RGB, DEFAULT_FG_RGB, palette_rgb, rgb_hex
from .params import Params
from .screen import rgb

if TYPE_CHECKING:
    from .screen import Screen


def _rgb_component(value: int) -> int:
    """SGR RGB components are 0–255. Values over clamp to 255 — a
    documented deviation (xterm masks them to 8 bits; ADR-0004); a
    negative value (-1, the empty `:` sub-parameter slot) clamps to 0.
    """
    return max(0, min(255, value))


def _rgb_param(params: Params, start: int) -> int:
    """SGR 38;2/48;2: the (r, g, b) components at `start` as one RGB
    cell color (ADR-0004)."""
    return rgb(
        _rgb_component(params.get(start)),
        _rgb_component(params.get(start + 1)),
        _rgb_component(params.get(start + 2)),
    )


def _osc_rgb(color: str) -> str:
    """`#rrggbb` → the xterm reply form `rgb:RRRR/GGGG/BBBB` (16-bit
    components — each 8-bit value doubled). A malformed color falls
    back to black rather than raising inside the reader thread."""
    if len(color) != 7 or not color.startswith("#"):
        return "rgb:0000/0000/0000"
    r = int(color[1:3], 16)
    g = int(color[3:5], 16)
    b = int(color[5:7], 16)
    return f"rgb:{r * 0x101:04x}/{g * 0x101:04x}/{b * 0x101:04x}"


class Emulator(Dispatcher):
    """The semantic layer: parser events in, screen operations out."""

    #: CSI dispatch table: (prefix, intermediates, final) → handler name.
    #: A sequence whose intermediates match no entry falls back to the
    #: bare final (no intermediates) — the xterm.js "bare final" rule.
    _CSI_DISPATCH: dict[tuple[str, str, str], str] = {
        ("", "", "h"): "_sm",  # SM — set ANSI modes
        ("", "", "l"): "_rm",  # RM — reset ANSI modes
        ("?", "", "h"): "_decset",  # DECSET — set DEC-private modes
        ("?", "", "l"): "_decrst",  # DECRST — reset DEC-private modes
        ("", "", "m"): "_sgr",  # SGR — graphic rendition
        ("", "", "r"): "_decstbm",  # DECSTBM — scroll region
        ("", "", "A"): "_cuu",  # CUU — cursor up
        ("", "", "B"): "_cud",  # CUD — cursor down
        ("", "", "C"): "_cuf",  # CUF — cursor forward
        ("", "", "D"): "_cub",  # CUB — cursor backward
        ("", "", "E"): "_cnl",  # CNL — cursor next line
        ("", "", "F"): "_cpl",  # CPL — cursor preceding line
        ("", "", "H"): "_cup",  # CUP — cursor position
        ("", "", "f"): "_cup",  # HVP — same as CUP
        ("", "", "G"): "_cha",  # CHA — cursor horizontal absolute
        ("", "", "d"): "_vpa",  # VPA — cursor vertical absolute
        ("", "", "J"): "_ed",  # ED — erase in display
        ("", "", "K"): "_el",  # EL — erase in line
        ("", "", "X"): "_ech",  # ECH — erase characters
        ("", "", "@"): "_ich",  # ICH — insert characters
        ("", "", "L"): "_il",  # IL — insert lines
        ("", "", "M"): "_dl",  # DL — delete lines
        ("", "", "P"): "_dch",  # DCH — delete characters
        ("", "", "S"): "_su",  # SU — scroll up
        ("", "", "T"): "_sd",  # SD — scroll down
        ("", "", "g"): "_tbc",  # TBC — tab clear
        ("", "", "I"): "_cht",  # CHT — cursor forward tabulation
        ("", "", "Z"): "_cbt",  # CBT — cursor backward tabulation
        ("", "", "s"): "_save",  # CSI s — save cursor (DECSC alias)
        ("", "", "u"): "_restore",  # CSI u — restore cursor (DECRC alias)
    }

    #: Escape dispatch table: (intermediates, final) → handler name.
    #: Exact match only — intermediate-bearing escapes (e.g. `ESC # 8`
    #: DECALN, Phase 3) parse-and-ignore until their step, so no
    #: bare-final fallback (xterm.js registers ESC handlers by exact
    #: key, unlike CSI's bare-final rule).
    _ESC_DISPATCH: dict[tuple[str, str], str] = {
        ("", "D"): "_ind",  # IND — index
        ("", "E"): "_nel",  # NEL — next line (CR + index)
        ("", "M"): "_ri",  # RI — reverse index
        ("", "n"): "_ls2",  # LS2 — shift to G2
        ("", "o"): "_ls3",  # LS3 — shift to G3
        ("", "~"): "_ls1r",  # LS1R — shift to G1
        ("", "}"): "_ls2r",  # LS2R — shift to G2
        ("", "|"): "_ls3r",  # LS3R — shift to G3
        ("", "H"): "_hts",  # HTS — set tab stop
        ("", "7"): "_decsc",  # DECSC — save cursor
        ("", "8"): "_decrc",  # DECRC — restore cursor
        ("#", "8"): "_decaln",  # DECALN — screen alignment test
    }

    def __init__(
        self,
        screen: "Screen",
        *,
        reply: Callable[[str], None] | None = None,
    ) -> None:
        """`reply`, when given, receives OSC query replies (BEL-
        terminated, xterm style) — the session wires it to the pty so
        the child sees its own terminal's colors."""
        self.screen = screen
        self._reply = reply
        self._default_fg = rgb_hex(*DEFAULT_FG_RGB)
        self._default_bg = rgb_hex(*DEFAULT_BG_RGB)
        self._palette = tuple(rgb_hex(*palette_rgb(index)) for index in range(256))
        #: OSC 12 — the cursor color (`#rrggbb`), None for the default
        #: inverted block (visible state: snapshots carry it).
        self._cursor_color: str | None = None

    def set_palette(self, fg: str, bg: str) -> None:
        """Replace the default foreground/background reported to OSC
        10/11 color queries (hex `#rrggbb` — the `QColor.name(HexRgb)`
        form the widget forwards)."""
        self._default_fg = fg
        self._default_bg = bg

    def chars(self, text: str) -> None:
        self.screen.print(text)

    def execute(self, code: int) -> None:
        """C0 controls: BS (0x08), HT (0x09), LF (0x0A), VT (0x0B),
        FF (0x0C), CR (0x0D), SO (0x0E, shift to G1), SI (0x0F, shift to
        G0). BEL (0x07) is swallowed; anything else is a no-op until its
        step."""
        if code == 0x08:
            self.screen.backspace()
        elif code == 0x09:
            self.screen.tab()
        elif code in (0x0A, 0x0B, 0x0C):
            self.screen.line_feed()
        elif code == 0x0D:
            self.screen.carriage_return()
        elif code == 0x0E:
            self.screen.shift_charset(1)  # SO — G1
        elif code == 0x0F:
            self.screen.shift_charset(0)  # SI — G0
        # BEL and the rest: no-op.

    def csi_dispatch(
        self, intermediates: str, prefix: str, params: Params, final: str
    ) -> None:
        handler = self._lookup_csi(intermediates, prefix, final)
        if handler is not None:
            getattr(self, handler)(params)

    def _lookup_csi(self, intermediates: str, prefix: str, final: str) -> str | None:
        name = self._CSI_DISPATCH.get((prefix, intermediates, final))
        if name is None and intermediates:
            name = self._CSI_DISPATCH.get((prefix, "", final))
        return name

    def _sm(self, params: Params) -> None:
        for i in range(params.count()):
            self.screen.set_mode(params.get(i))

    def _rm(self, params: Params) -> None:
        for i in range(params.count()):
            self.screen.reset_mode(params.get(i))

    def _decset(self, params: Params) -> None:
        """DECSET. 47/1047/1049 switch to the alternate screen; 1049
        saves the cursor first (xterm.js: saveCursor + fall-through);
        1048 saves only. Everything else lands in the mode registry."""
        for i in range(params.count()):
            mode = params.get(i)
            if mode == 1049:
                self.screen.save_state()
                self.screen.enter_alt_screen()
            elif mode in (47, 1047):
                self.screen.enter_alt_screen()
            elif mode == 1048:
                self.screen.save_state()
            else:
                self.screen.set_mode(mode, private=True)

    def _decrst(self, params: Params) -> None:
        """DECRST. 47/1047/1049 leave the alternate screen (clearing
        it); 1049 restores the cursor after (xterm.js: activateNormal
        + restoreCursor); 1048 restores only. Everything else lands in
        the mode registry."""
        for i in range(params.count()):
            mode = params.get(i)
            if mode == 1049:
                self.screen.leave_alt_screen()
                self.screen.restore_state()
            elif mode in (47, 1047):
                self.screen.leave_alt_screen()
            elif mode == 1048:
                self.screen.restore_state()
            else:
                self.screen.reset_mode(mode, private=True)

    def _decstbm(self, params: Params) -> None:
        """DECSTBM: 1-based rows, `CSI r` (no params) resets to full
        screen; an explicit 0 behaves like 1, and the screen clamps."""
        top = params.get(0) or 1
        bottom = params.get(1) or self.screen.lines
        self.screen.set_scroll_region(top - 1, bottom - 1)

    def _cuu(self, params: Params) -> None:
        self.screen.cursor_up(params.get(0) or 1)

    def _cud(self, params: Params) -> None:
        self.screen.cursor_down(params.get(0) or 1)

    def _cuf(self, params: Params) -> None:
        self.screen.cursor_forward(params.get(0) or 1)

    def _cub(self, params: Params) -> None:
        self.screen.cursor_backward(params.get(0) or 1)

    def _cnl(self, params: Params) -> None:
        self.screen.cursor_next_line(params.get(0) or 1)

    def _cpl(self, params: Params) -> None:
        self.screen.cursor_preceding_line(params.get(0) or 1)

    def _cup(self, params: Params) -> None:
        """CUP/HVP: 1-based; a single parameter moves to that row in
        column 0 (xterm.js); missing parameters default to 1."""
        row = (params.get(0) or 1) - 1
        col = (params.get(1) or 1) - 1 if params.count() >= 2 else 0
        self.screen.set_cursor(col, row)

    def _cha(self, params: Params) -> None:
        """CHA: cursor to column n (1-based, default 1) of the current
        row — the row never changes (xterm.js cursorPosition)."""
        col = (params.get(0) or 1) - 1
        self.screen.set_cursor(col, self.screen.cursor.y)

    def _vpa(self, params: Params) -> None:
        """VPA: cursor to row n (1-based, default 1), column unchanged
        (xterm.js verticalPosition — origin-relative under DECOM)."""
        row = (params.get(0) or 1) - 1
        self.screen.set_cursor(self.screen.cursor.x, row)

    def _ed(self, params: Params) -> None:
        """ED: 0/1/2 erase in display; 3 clears the scrollback
        (ADR-0006 — the only runtime erasure of history)."""
        mode = params.get(0)
        if mode == 3:
            self.screen.clear_scrollback()
        else:
            self.screen.erase_in_display(mode)

    def _el(self, params: Params) -> None:
        self.screen.erase_in_line(params.get(0))

    def _ech(self, params: Params) -> None:
        self.screen.erase_chars(params.get(0) or 1)

    def _ich(self, params: Params) -> None:
        self.screen.insert_chars(params.get(0) or 1)

    def _il(self, params: Params) -> None:
        self.screen.insert_lines(params.get(0) or 1)

    def _dl(self, params: Params) -> None:
        self.screen.delete_lines(params.get(0) or 1)

    def _dch(self, params: Params) -> None:
        self.screen.delete_chars(params.get(0) or 1)

    def _su(self, params: Params) -> None:
        self.screen.scroll_up(params.get(0) or 1)

    def _sd(self, params: Params) -> None:
        self.screen.scroll_down(params.get(0) or 1)

    def _tbc(self, params: Params) -> None:
        self.screen.clear_tab_stop(params.get(0))

    def _cht(self, params: Params) -> None:
        self.screen.tab_forward(params.get(0) or 1)

    def _cbt(self, params: Params) -> None:
        self.screen.tab_backward(params.get(0) or 1)

    def _save(self, params: Params) -> None:
        self.screen.save_state()

    def _restore(self, params: Params) -> None:
        self.screen.restore_state()

    def _sgr(self, params: Params) -> None:
        screen = self.screen
        i = 0
        count = params.count()
        while i < count:
            value = params.get(i)
            if value == 0:
                screen.reset_rendition()
            elif value == 1:
                screen.set_bold()
            elif value == 2:
                screen.set_dim()
            elif value == 3:
                screen.set_italic()
            elif value == 4:
                screen.set_underline()
            elif value in (5, 6):
                screen.set_blink()  # 6 rapid blink collapses to blink
            elif value == 7:
                screen.set_reverse()
            elif value == 8:
                screen.set_hidden()
            elif value == 9:
                screen.set_strike()
            elif value == 21:
                # Double underline collapses to the boolean underline
                screen.set_underline()
            elif value == 22:
                screen.set_bold(False)
                screen.set_dim(False)
            elif value == 23:
                screen.set_italic(False)
            elif value == 24:
                screen.set_underline(False)
            elif value == 25:
                screen.set_blink(False)
            elif value == 27:
                screen.set_reverse(False)
            elif value == 28:
                screen.set_hidden(False)
            elif value == 29:
                screen.set_strike(False)
            elif 30 <= value <= 37:
                # SGR codes map onto the 256-color palette's first entries:
                # the cell stores the palette index, not the raw SGR code.
                screen.set_fg(value - 30)
            elif value == 38:
                # Extended foreground: `38;5;n` / `38;2;r;g;b` and the
                # colon forms `38:5:n` / `38:2:r:g:b` / `38:2:cs:r:g:b`.
                i += self._sgr_extended(params, i, screen.set_fg)
            elif value == 39:
                screen.set_fg(-1)
            elif 40 <= value <= 47:
                screen.set_bg(value - 40)
            elif value == 48:
                # Extended background — same syntaxes as 38.
                i += self._sgr_extended(params, i, screen.set_bg)
            elif value == 49:
                screen.set_bg(-1)
            elif 90 <= value <= 97:
                screen.set_fg(value - 90 + 8)
            elif 100 <= value <= 107:
                screen.set_bg(value - 100 + 8)
            elif value == 53:
                screen.set_overline()
            elif value == 55:
                screen.set_overline(False)
            # Anything else (fonts 10–20, 26, 51/52/54, 56+, 59,
            # 58 extended colors): parse-and-ignore.
            i += 1

    def _sgr_extended(
        self, params: Params, i: int, set_color: Callable[[int], None]
    ) -> int:
        """SGR 38/48 — extended colors — for the parameter group at `i`.

        Handles both syntaxes:
        - `38;5;n` / `38;2;r;g;b` — semicolon-separated params (the
          classic form), consuming 2 or 4 following parameters.
        - `38:5:n` / `38:2:r:g:b` colon sub-params — xterm's
          newer form, self-contained in the group: `(5, n)` a palette
          index, `(2, r, g, b)` or `(2, cs, r, g, b)` RGB (cs, the
          color space, is accepted and ignored).

        A truncated or malformed sequence leaves the color untouched —
        for the semicolon form the leftover components fall through and
        re-parse as standalone SGR codes (xterm.js-verbatim: a truncated
        `38;2;1;2` sets bold + dim); for the colon form the group
        consumes nothing extra. Returns the number of *additional*
        parameters consumed (0 when nothing matched)."""
        sub = params.subparams(i)
        if sub:
            # Colon form: everything lives in this group's sub-params.
            if sub[0] == 2 and len(sub) in (4, 5):
                r, g, b = (sub[1:] if len(sub) == 4 else sub[2:])
                set_color(rgb(_rgb_component(r), _rgb_component(g), _rgb_component(b)))
            elif sub[0] == 5 and len(sub) == 2 and 0 <= sub[1] <= 255:
                set_color(sub[1])
            return 0
        count = params.count()
        if i + 2 < count and params.get(i + 1) == 5:
            set_color(params.get(i + 2))
            return 2
        if i + 4 < count and params.get(i + 1) == 2:
            set_color(_rgb_param(params, i + 2))
            return 4
        return 0

    def escape_dispatch(self, intermediates: str, final: str) -> None:
        """Escape sequences (no parameters): look up (intermediates,
        final) in the escape table and run the handler. Intermediates
        never fall back to the bare final (exact match)."""
        handler = self._lookup_esc(intermediates, final)
        if handler is not None:
            getattr(self, handler)()

    def _lookup_esc(self, intermediates: str, final: str) -> str | None:
        """Exact (intermediates, final) match — no bare-final fallback:
        `ESC # 8` (DECALN) must not trigger DECRC."""
        return self._ESC_DISPATCH.get((intermediates, final))

    def _ind(self) -> None:
        self.screen.index()  # IND

    def _nel(self) -> None:
        """NEL: CR + index (xterm.js nextLine: x = 0, then index) —
        column 0, then down one line, scrolling the region at its
        bottom. Unlike LF, an index does not clear the wrapped marker."""
        self.screen.carriage_return()
        self.screen.index()

    def _ri(self) -> None:
        self.screen.reverse_index()  # RI

    def _ls2(self) -> None:
        self.screen.shift_charset(2)  # LS2 — G2

    def _ls3(self) -> None:
        self.screen.shift_charset(3)  # LS3 — G3

    def _ls1r(self) -> None:
        self.screen.shift_charset(1)  # LS1R — G1

    def _ls2r(self) -> None:
        self.screen.shift_charset(2)  # LS2R — G2

    def _ls3r(self) -> None:
        self.screen.shift_charset(3)  # LS3R — G3

    def _hts(self) -> None:
        self.screen.set_tab_stop()  # HTS

    def _decsc(self) -> None:
        self.screen.save_state()  # DECSC

    def _decrc(self) -> None:
        self.screen.restore_state()  # DECRC

    def _decaln(self) -> None:
        self.screen.decaln()  # DECALN — screen alignment test

    def designate_charset(self, designator: str, charset: str) -> None:
        self.screen.designate_charset(designator, charset)

    def osc_dispatch(self, payload: str) -> None:
        """OSC dispatch — split on `;`, dispatch on the first field.

        Phase 5, color queries: `4` (palette), `10` (fg), `11` (bg),
        `12` (cursor) — the queries TUI apps (opencode, vim, …) send
        to detect the terminal's theme and pick their own cursor
        color. Set forms (`4;i;spec`, `10;spec`, `11;spec`) parse-and-
        ignore for now (palette mutation is a follow-up); `12;spec`
        and `112` (reset) apply — the cursor color is visible state.
        Everything else stays a no-op until its step."""
        fields = payload.split(";")
        command = fields[0]
        if command == "4":
            self._osc_color_query(fields)
        elif command in ("10", "11") and len(fields) >= 2 and fields[1] == "?":
            color = self._default_fg if command == "10" else self._default_bg
            self._osc_reply(f"{command};{_osc_rgb(color)}")
        elif command == "12":
            self._osc_cursor_color(fields)
        elif command == "112":
            # OSC 112 — reset the cursor color to the terminal default.
            self._cursor_color = None

    def _osc_cursor_color(self, fields: list[str]) -> None:
        """OSC 12 — the cursor color, set or query. Set forms
        (`12;#rrggbb`, `12;rgb:rrrr/gggg/bbbb`) replace the color the
        renderer paints the cursor block with — apps (opencode, vim)
        set their own per-theme caret color, and honoring it keeps the
        block visible in light themes (where the default inverted
        block would take the cell's white foreground). `12;?` reports
        the current color back (xterm style); unset stays silent."""
        if len(fields) >= 2 and fields[1] == "?":
            if self._cursor_color is not None:
                self._osc_reply(f"12;{_osc_rgb(self._cursor_color)}")
            return
        if len(fields) < 2:
            return
        spec = fields[1]
        if len(spec) == 7 and spec.startswith("#"):
            self._cursor_color = spec
        elif spec.startswith("rgb:"):
            # rgb:RRRR/GGGG/BBBB — 16-bit components, scaled to 8-bit.
            # Any other arity (rgb:RRRR/GGGG, …) is malformed — ignore
            # rather than store an unparsable `#rrggbb`.
            parts = spec[4:].split("/")
            if len(parts) == 3:
                try:
                    self._cursor_color = "#" + "".join(
                        f"{int(p, 16) >> 8:02x}" for p in parts
                    )
                except ValueError:
                    pass

    @property
    def cursor_color(self) -> str | None:
        """The OSC 12 cursor color (`#rrggbb`), None for the default
        inverted cursor — the session mirrors it into snapshots."""
        return self._cursor_color

    def _osc_color_query(self, fields: list[str]) -> None:
        """OSC 4 query forms — `4;?` (all 16), `4;i;?` (one index),
        `4;i1;?;i2;?` (several): reply each queried palette color.
        Set forms carry no `?` — parse-and-ignore."""
        if "?" not in fields:
            return
        indices = [int(field) for field in fields[1:] if field.isdigit()] or list(
            range(16)
        )
        replies = [
            f"{index};{_osc_rgb(self._palette[index])}"
            for index in indices
            if 0 <= index < 256
        ]
        if replies:
            self._osc_reply(f"4;{';'.join(replies)}")

    def _osc_reply(self, payload: str) -> None:
        """Send an OSC reply to the child (BEL-terminated, xterm style);
        a missing reply callback (headless tests) silently drops it."""
        if self._reply is not None:
            self._reply(f"\x1b]{payload}\x07")
