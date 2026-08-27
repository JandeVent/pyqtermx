# -*- coding: utf-8 -*-
# Copyright (C) 2018-2026 Connet Information Technology Company, Shanghai.
"""Input encoding — QKeyEvent → terminal bytes (Slice B, spec Q8).

The GUI layer's input path, unit-tested with synthetic QKeyEvents. Pure:
mode state is passed in (the widget mirrors it from snapshots — it never
reads the model, ADR-0005): `dec_ckm` (DECCKM `?1`), `bracketed_paste`
(`?2004`), and `scrollback_len` for the PgUp/PgDn viewport policy
(history exists implies the normal screen — the alt screen has none).

`encode_key` returns the bytes to send to the child, or `None` when the
GUI must act itself instead: PgUp/PgDn scrolling the viewport (posted as
a `scroll` command), paste (clipboard → `encode_paste`), copy (no-op).
Every sent key is followed by a `scroll_to_bottom()` post (spec Q6).

Signals: the terminal's only signal channel is control characters — the
child's tty line discipline turns them into SIGINT/SIGQUIT/SIGTSTP
(⌃+C / ⌃+\\ / ⌃+Z / ⌃+Y, ISIG), stops output (⌃+S, IXON), or reports
EOF (⌃+D). The encoder derives these from the *key*, so a text-less
⌃+letter event (macOS) still sends the right byte.

Combo keys (the modern-terminal set): Shift+Tab is back-tab (CSI Z),
Insert/Delete are CSI 2~/3~, F1–F12 are SS3/CSI, ⌥ prefixes editing
keys with ESC, and modifiers on cursor/function keys use xterm's
CSI 1;N modifier code (1+shift+2·alt+4·ctrl).
"""

from __future__ import annotations

import sys

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QKeyEvent

#: xterm modifier codes (CSI 1;N X): 1 + shift(1) + alt(2) + ctrl(4).
_MOD_SHIFT = 1
_MOD_ALT = 2
_MOD_CTRL = 4

#: The modifier Qt reports for the physical Ctrl key. On macOS ⌃ maps to
#: MetaModifier, while ControlModifier is the ⌘ Command key — which must
#: never reach the terminal.
CTRL_MOD = (
    Qt.KeyboardModifier.MetaModifier
    if sys.platform == "darwin"
    else Qt.KeyboardModifier.ControlModifier
)

#: Key → CSI final for the cursor keys (application mode adds SS3).
#: `event.key()` is an `int` in the PyQt6 stubs; the members are IntEnums.
_CURSOR_FINALS: dict[int, str] = {
    Qt.Key.Key_Up: "A",
    Qt.Key.Key_Down: "B",
    Qt.Key.Key_Right: "C",
    Qt.Key.Key_Left: "D",
    Qt.Key.Key_Home: "H",
    Qt.Key.Key_End: "F",
}

#: The arrow keys — the only cursor keys that switch to SS3 under
#: application cursor mode (Home/End stay CSI, matching encode_key).
_ARROW_KEYS = (
    Qt.Key.Key_Up,
    Qt.Key.Key_Down,
    Qt.Key.Key_Right,
    Qt.Key.Key_Left,
)

#: ⌃+symbol/digit keys → control codes (xterm's table on the US
#: layout). Letters need no table: Key_A..Key_Z are the ASCII codes, so
#: `& 0x1f` is the control character — ⌃+C = VINTR 0x03 (SIGINT), ⌃+Z =
#: VSUSP 0x1a (SIGTSTP), … . ⌃+\ is the one symbol key in the signal
#: set: VQUIT 0x1c (SIGQUIT).
_CTRL_KEY_CODES: dict[int, bytes] = {
    Qt.Key.Key_Backslash: b"\x1c",  # VQUIT (SIGQUIT)
    Qt.Key.Key_BracketLeft: b"\x1b",  # ESC
    Qt.Key.Key_BracketRight: b"\x1d",  # GS
    Qt.Key.Key_AsciiCircum: b"\x1e",  # RS
    Qt.Key.Key_Underscore: b"\x1f",  # US
    Qt.Key.Key_Slash: b"\x1f",  # US (0x2f & 0x1f would be SI — xterm maps /)
    Qt.Key.Key_Question: b"\x7f",  # DEL
    Qt.Key.Key_Minus: b"\x1f",  # US (xterm maps ⌃+- to 0x1f, not 0x0d)
    Qt.Key.Key_Space: b"\x00",  # NUL
    Qt.Key.Key_At: b"\x00",  # ⌃+@ = NUL
    Qt.Key.Key_2: b"\x00",  # ⌃+2 = NUL
    Qt.Key.Key_3: b"\x1b",  # ESC
    Qt.Key.Key_4: b"\x1c",  # FS
    Qt.Key.Key_5: b"\x1d",  # GS
    Qt.Key.Key_6: b"\x1e",  # RS
    Qt.Key.Key_7: b"\x1f",  # US
    Qt.Key.Key_8: b"\x7f",  # DEL
}

#: Insert/Delete → CSI finals (xterm). Shift+Insert is paste — handled
#: before this table.
_EDIT_FINALS: dict[int, str] = {
    Qt.Key.Key_Insert: "2~",
    Qt.Key.Key_Delete: "3~",
}

#: F1–F12 → xterm sequences. F1–F4 send SS3 P..S plain; F5+ send
#: CSI n~. Any modifier switches to CSI 1;N + final.
_FKEY_FINALS: dict[int, str] = {
    Qt.Key.Key_F1: "P",
    Qt.Key.Key_F2: "Q",
    Qt.Key.Key_F3: "R",
    Qt.Key.Key_F4: "S",
    Qt.Key.Key_F5: "15~",
    Qt.Key.Key_F6: "17~",
    Qt.Key.Key_F7: "18~",
    Qt.Key.Key_F8: "19~",
    Qt.Key.Key_F9: "20~",
    Qt.Key.Key_F10: "21~",
    Qt.Key.Key_F11: "23~",
    Qt.Key.Key_F12: "24~",
}


def _ctrl_letter(qkey: int) -> bytes | None:
    """⌃+A..Z → the ASCII control character (⌃+C = VINTR 0x03)."""
    if Qt.Key.Key_A <= qkey <= Qt.Key.Key_Z:
        return bytes([qkey & 0x1F])
    return None


def _modifier_code(shift: bool, alt: bool, ctrl: bool) -> int:
    """xterm CSI modifier code: 1 + shift(1) + alt(2) + ctrl(4)."""
    return 1 + (1 if shift else 0) + (2 if alt else 0) + (4 if ctrl else 0)


def encode_arrow_key(qkey: int, *, dec_ckm: bool = False) -> bytes:
    """A plain arrow key (no modifiers) — the wheel → cursor policy
    for full-screen apps without mouse tracking: CSI final, or SS3 in
    application cursor mode (DECCKM). Mirrors encode_key's arrow
    branch, which stays the source for real key events (`event.key()`
    is an `int` in the PyQt6 stubs)."""
    if qkey not in _ARROW_KEYS:
        raise ValueError(f"not an arrow key: {qkey}")
    final = _CURSOR_FINALS[qkey]
    if dec_ckm:
        return b"\x1bO" + final.encode()
    return b"\x1b[" + final.encode()


def encode_key(
    event: QKeyEvent,
    *,
    dec_ckm: bool = False,
    scrollback_len: int = 0,
) -> bytes | None:
    """Encode a key press into terminal bytes, or None for GUI-side
    actions (viewport scroll, paste, copy)."""
    mods = event.modifiers()
    if sys.platform == "darwin" and mods & Qt.KeyboardModifier.ControlModifier:
        # ⌘ is Command on macOS — application shortcuts (paste/copy),
        # never a control code for the shell.
        return None
    ctrl = bool(mods & CTRL_MOD)
    shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
    alt = bool(mods & Qt.KeyboardModifier.AltModifier)
    text = event.text()
    qkey = event.key()

    # Paste/copy shortcuts come first: they must not fall through to the
    # Ctrl+letter control-code path.
    if ctrl and shift and qkey in (Qt.Key.Key_V, Qt.Key.Key_C):
        return None  # paste (clipboard) or copy (no-op) — GUI-side
    if shift and not ctrl and not alt and qkey == Qt.Key.Key_Insert:
        return None  # Shift+Insert paste — GUI-side

    # PgUp/PgDn: scroll the viewport when history exists (normal screen);
    # in the alt screen (no history) they become CSI 5~/6~.
    if qkey == Qt.Key.Key_PageUp:
        return None if scrollback_len > 0 else b"\x1b[5~"
    if qkey == Qt.Key.Key_PageDown:
        return None if scrollback_len > 0 else b"\x1b[6~"

    if qkey in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
        if shift:
            return b"\n"
        return b"\x1b\r" if alt else b"\r"
    if qkey in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
        # Shift+Tab is back-tab (CSI Z — Qt reports Key_Backtab on some
        # platforms); ctrl/alt add the usual modifier code. Plain Tab is
        # the only n==1 case.
        shift = shift or qkey == Qt.Key.Key_Backtab
        n = _modifier_code(shift, alt, ctrl)
        if n == 2:
            return b"\x1b[Z"
        if n == 1:
            return b"\t"
        return f"\x1b[1;{n}Z".encode()
    if qkey == Qt.Key.Key_Backspace:
        return b"\x1b\x7f" if alt else b"\x7f"
    if qkey in _EDIT_FINALS:
        n = _modifier_code(shift, alt, ctrl)
        if n == 1:
            return b"\x1b[" + _EDIT_FINALS[qkey].encode()
        # xterm format: CSI key_code;modifier ~  (not CSI 1;modifier key_code~)
        key_code = _EDIT_FINALS[qkey].rstrip("~")
        return f"\x1b[{key_code};{n}~".encode()
    if qkey in _FKEY_FINALS:
        n = _modifier_code(shift, alt, ctrl)
        if n == 1 and qkey <= Qt.Key.Key_F4:
            return b"\x1bO" + _FKEY_FINALS[qkey].encode()  # SS3 P..S
        if n == 1:
            return b"\x1b[" + _FKEY_FINALS[qkey].encode()
        return f"\x1b[1;{n}{_FKEY_FINALS[qkey]}".encode()

    final = _CURSOR_FINALS.get(qkey)
    if final is not None:
        n = _modifier_code(shift, alt, ctrl)
        if n == 1 and qkey in _ARROW_KEYS:
            return encode_arrow_key(qkey, dec_ckm=dec_ckm)  # SS3 in app mode
        if n == 1:
            return b"\x1b[" + final.encode()
        return f"\x1b[1;{n}{final}".encode()

    if ctrl:
        if alt:
            # xterm metaSendsEscape with ctrl: ESC + the control code.
            base = _CTRL_KEY_CODES.get(qkey)
            if base is None:
                base = _ctrl_letter(qkey)
            if base is not None:
                return b"\x1b" + base
        if len(text) == 1 and ord(text[0]) < 0x20:
            return text.encode()  # Ctrl+letter → the control code itself
        # ⌃+letter events often carry no text (macOS) — derive the
        # control code from the key, so Ctrl+C is always VINTR (SIGINT)
        # and Ctrl+\ always VQUIT (SIGQUIT). The tty line discipline
        # turns these bytes into signals; the terminal only sends them.
        code = _CTRL_KEY_CODES.get(qkey)
        if code is None:
            code = _ctrl_letter(qkey)
        if code is not None:
            return code
    if alt and not ctrl and len(text) == 1 and ord(text[0]) >= 0x20:
        return b"\x1b" + text.encode()  # Alt+letter → ESC + char
    if text:
        return text.encode()
    return None  # dead keys, unhandled keys


def encode_paste(text: str, *, bracketed_paste: bool = False) -> bytes:
    """The paste payload: raw UTF-8, or wrapped in bracketed-paste
    markers when the app requested `?2004` (spec Q8)."""
    data = text.encode()
    if bracketed_paste:
        return b"\x1b[200~" + data + b"\x1b[201~"
    return data


def _wheel_mods(mods: int) -> int:
    """Wheel buttons leave no room for the standard modifier bits (the
    X10 wheel codes 4/5 collide with shift/meta) — the xterm convention
    maps shift→2, ctrl→4, meta→8 (SGR 66/67 shift-wheel, 68/69
    ctrl-wheel, 70/71 shift-ctrl)."""
    return ((mods & 4) >> 1) | ((mods & 16) >> 2) | (mods & 8)


def encode_mouse_x10(x: int, y: int, button: int, *, action: str, mods: int = 0) -> bytes:
    """X10 mouse (DECSET ?1000): `CSI M` + three bytes, each +32 —
    button, column, row. `button` is 0 left, 1 middle, 2 right; the
    release code is 3, and "motion" adds 32. `mods` sums the xterm
    modifier bits (4 shift, 8 meta/alt, 16 ctrl) into the button byte.
    Coordinates are 1-based; the caller clamps them to the grid. Wheel
    buttons are 4/5 (up/down — the X11 wheel convention xterm reports)
    with the `_wheel_mods` mapping."""
    if action == "release":
        b = 3 + mods
    elif action == "motion":
        b = button + 32 + mods
    elif action == "press":
        b = button + mods
    elif action == "wheel_up":
        b = 4 + _wheel_mods(mods)
    elif action == "wheel_down":
        b = 5 + _wheel_mods(mods)
    else:  # unreachable: the widget only sends the actions above
        raise ValueError(f"X10 mouse cannot encode {action}")
    return b"\x1b[M" + bytes((32 + b, 32 + x, 32 + y))


def encode_sgr_mouse(x: int, y: int, button: int, *, action: str, mods: int = 0) -> bytes:
    """SGR mouse (DECSET ?1006): `CSI < b ; x ; y M`, final `m` for
    release. `action` selects the flags: "motion" adds 32, "wheel_up"
    64, "wheel_down" 65 (button ignored — `_wheel_mods` maps the
    modifiers); `mods` sums the xterm modifier bits (4 shift, 8
    meta/alt, 16 ctrl). Coordinates are 1-based; the caller clamps
    them to the grid."""
    if action == "release":
        code, final = button + mods, "m"
    elif action == "motion":
        code, final = 32 + button + mods, "M"
    elif action == "wheel_up":
        code, final = 64 + _wheel_mods(mods), "M"
    elif action == "wheel_down":
        code, final = 65 + _wheel_mods(mods), "M"
    else:  # press
        code, final = button + mods, "M"
    return f"\x1b[<{code};{x};{y}{final}".encode()
