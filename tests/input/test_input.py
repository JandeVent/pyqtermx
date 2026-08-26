# -*- coding: utf-8 -*-
# Copyright (C) 2018-2026 Connet Information Technology Company, Shanghai.
"""Phase 4 — Input encoding (Slice B, Q8): QKeyEvent → terminal bytes.

The seam: the pure `encode_key(event, ...)` module, unit-tested with
synthetic QKeyEvents — no widget, no QApplication needed. Mode state is
passed in (the GUI mirrors it from snapshots; it never reads the model):
`dec_ckm` (?1 application cursor mode), `bracketed_paste` (?2004), and
`scrollback_len` for the PgUp/PgDn viewport policy (history exists
implies the normal screen — the alt screen has none, ADR-0006).

Return value contract: `bytes` to send to the child, or `None` when the
GUI must act itself (PgUp/PgDn scrolling the viewport, paste, copy).
"""

from __future__ import annotations

import sys

import pytest
from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QKeyEvent

from pyqtermx.input import (
    encode_key,
    encode_mouse_x10,
    encode_paste,
    encode_sgr_mouse,
)


def key(
    code: Qt.Key,
    text: str = "",
    modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier,
) -> QKeyEvent:
    return QKeyEvent(QEvent.Type.KeyPress, code, modifiers, text)


def ctrl() -> Qt.KeyboardModifier:
    """The modifier Qt reports for the physical Ctrl key: ⌃ (MetaModifier)
    on macOS, ControlModifier elsewhere."""
    return (
        Qt.KeyboardModifier.MetaModifier
        if sys.platform == "darwin"
        else Qt.KeyboardModifier.ControlModifier
    )


ALT = Qt.KeyboardModifier.AltModifier
SHIFT = Qt.KeyboardModifier.ShiftModifier


# -- Printables -----------------------------------------------------------


def test_printable_character() -> None:
    assert encode_key(key(Qt.Key.Key_A, "a")) == b"a"


def test_shift_gives_uppercase() -> None:
    assert encode_key(key(Qt.Key.Key_A, "A", SHIFT)) == b"A"


def test_utf8_text() -> None:
    assert encode_key(key(Qt.Key.Key_E, "héllo")) == "héllo".encode()


def test_dead_key_without_text_is_none() -> None:
    assert encode_key(key(Qt.Key.Key_Dead_Grave, "")) is None


# -- Editing keys ---------------------------------------------------------


def test_enter_is_carriage_return() -> None:
    assert encode_key(key(Qt.Key.Key_Return, "\r")) == b"\r"
    assert encode_key(key(Qt.Key.Key_Enter, "\r")) == b"\r"


def test_shift_enter_is_line_feed() -> None:
    assert encode_key(key(Qt.Key.Key_Return, "\r", SHIFT)) == b"\n"
    assert encode_key(key(Qt.Key.Key_Enter, "\r", SHIFT)) == b"\n"


def test_tab_and_backspace() -> None:
    assert encode_key(key(Qt.Key.Key_Tab, "\t")) == b"\t"
    assert encode_key(key(Qt.Key.Key_Backspace, "\x7f")) == b"\x7f"


# -- Modifier encodings ---------------------------------------------------


def test_ctrl_letter_is_control_code() -> None:
    assert encode_key(key(Qt.Key.Key_A, "\x01", ctrl())) == b"\x01"
    assert encode_key(key(Qt.Key.Key_C, "\x03", ctrl())) == b"\x03"


# -- Control characters: the terminal→program signal set -----------------
# A terminal's only signal channel is control characters; the child's
# tty line discipline turns them into signals (ISIG), flow control
# (IXON), or EOF. Every entry must survive a text-less ⌃+event — the
# live macOS case that dropped Ctrl+C entirely (htop never got SIGINT).

SIGNAL_MATRIX: dict[Qt.Key, bytes] = {
    Qt.Key.Key_C: b"\x03",  # VINTR → SIGINT
    Qt.Key.Key_Backslash: b"\x1c",  # VQUIT → SIGQUIT
    Qt.Key.Key_Z: b"\x1a",  # VSUSP → SIGTSTP
    Qt.Key.Key_Y: b"\x19",  # VDSUSP → SIGTSTP (delayed)
    Qt.Key.Key_D: b"\x04",  # VEOF → EOF on read
    Qt.Key.Key_S: b"\x13",  # VSTOP → output stops (IXON)
    Qt.Key.Key_Q: b"\x11",  # VSTART → output resumes
    Qt.Key.Key_O: b"\x0f",  # VDISCARD → discard output
    Qt.Key.Key_R: b"\x12",  # VREPRINT
    Qt.Key.Key_V: b"\x16",  # VLNEXT
    Qt.Key.Key_W: b"\x17",  # VWERASE
    Qt.Key.Key_U: b"\x15",  # VKILL
    Qt.Key.Key_H: b"\x08",  # VERASE
    Qt.Key.Key_T: b"\x14",  # VSTATUS (macOS)
}


def test_ctrl_signal_set_with_text() -> None:
    # Qt events carry the control char in text (X11-style events).
    for code, expected in SIGNAL_MATRIX.items():
        assert encode_key(key(code, expected.decode(), ctrl())) == expected


def test_ctrl_signal_set_without_text() -> None:
    # macOS ⌃+letter events carry no text — the code must come from
    # the key alone (the regression that broke Ctrl+C on htop).
    for code, expected in SIGNAL_MATRIX.items():
        assert encode_key(key(code, "", ctrl())) == expected


def test_ctrl_letters_cover_the_full_ascii_range() -> None:
    for i in range(26):
        qkey = Qt.Key.Key_A + i
        assert encode_key(key(qkey, "", ctrl())) == bytes([i + 1])


def test_copy_shortcut_without_text_is_still_noop() -> None:
    # The ⌃⇧C copy shortcut must keep winning over the key-derived
    # control code even when the event carries no text.
    assert encode_key(key(Qt.Key.Key_C, "", ctrl() | SHIFT)) is None


# -- Combo keys: the modern-terminal set ---------------------------------
# Shift+Tab is back-tab (CSI Z), Insert/Delete are CSI 2~/3~, F-keys are
# SS3/CSI, Alt prefixes editing keys with ESC, and Ctrl applies to the
# symbol/digit keys too. All of these arrive text-less in live events.

def test_shift_tab_is_backtab_csi_z() -> None:
    # The regression: Qt reports Key_Backtab (no text) for Shift+Tab.
    assert encode_key(key(Qt.Key.Key_Backtab, "")) == b"\x1b[Z"
    assert encode_key(key(Qt.Key.Key_Tab, "\t", SHIFT)) == b"\x1b[Z"


def test_tab_with_modifiers() -> None:
    assert encode_key(key(Qt.Key.Key_Tab, "\t")) == b"\t"
    assert encode_key(key(Qt.Key.Key_Tab, "\t", ctrl())) == b"\x1b[1;5Z"
    assert encode_key(key(Qt.Key.Key_Tab, "\t", ALT)) == b"\x1b[1;3Z"
    assert encode_key(key(Qt.Key.Key_Backtab, "", ctrl())) == b"\x1b[1;6Z"
    assert encode_key(key(Qt.Key.Key_Backtab, "", ALT)) == b"\x1b[1;4Z"
    assert encode_key(key(Qt.Key.Key_Tab, "\t", ctrl() | ALT)) == b"\x1b[1;7Z"


def test_insert_and_delete() -> None:
    assert encode_key(key(Qt.Key.Key_Insert, "")) == b"\x1b[2~"
    assert encode_key(key(Qt.Key.Key_Delete, "")) == b"\x1b[3~"
    assert encode_key(key(Qt.Key.Key_Delete, "", ctrl())) == b"\x1b[1;53~"
    assert encode_key(key(Qt.Key.Key_Insert, "", ALT)) == b"\x1b[1;32~"


def test_function_keys() -> None:
    assert encode_key(key(Qt.Key.Key_F1, "")) == b"\x1bOP"
    assert encode_key(key(Qt.Key.Key_F4, "")) == b"\x1bOS"
    assert encode_key(key(Qt.Key.Key_F5, "")) == b"\x1b[15~"
    assert encode_key(key(Qt.Key.Key_F10, "")) == b"\x1b[21~"
    assert encode_key(key(Qt.Key.Key_F12, "")) == b"\x1b[24~"
    assert encode_key(key(Qt.Key.Key_F1, "", SHIFT)) == b"\x1b[1;2P"
    assert encode_key(key(Qt.Key.Key_F5, "", ctrl())) == b"\x1b[1;515~"
    assert encode_key(key(Qt.Key.Key_F10, "", ALT)) == b"\x1b[1;321~"


def test_alt_prefixes_editing_keys() -> None:
    assert encode_key(key(Qt.Key.Key_Backspace, "\x7f", ALT)) == b"\x1b\x7f"
    assert encode_key(key(Qt.Key.Key_Return, "\r", ALT)) == b"\x1b\r"


def test_ctrl_alt_letter_is_esc_control() -> None:
    # xterm metaSendsEscape with ctrl: ESC + the control code.
    assert encode_key(key(Qt.Key.Key_C, "", ctrl() | ALT)) == b"\x1b\x03"
    assert encode_key(key(Qt.Key.Key_C, "\x03", ctrl() | ALT)) == b"\x1b\x03"


def test_ctrl_symbols_and_digits() -> None:
    assert encode_key(key(Qt.Key.Key_Space, "", ctrl())) == b"\x00"  # NUL
    assert encode_key(key(Qt.Key.Key_At, "", ctrl())) == b"\x00"  # ⌃+@
    assert encode_key(key(Qt.Key.Key_2, "", ctrl())) == b"\x00"
    assert encode_key(key(Qt.Key.Key_3, "", ctrl())) == b"\x1b"  # ESC
    assert encode_key(key(Qt.Key.Key_Minus, "", ctrl())) == b"\x1f"  # US
    assert encode_key(key(Qt.Key.Key_Slash, "", ctrl())) == b"\x1f"  # US
    assert encode_key(key(Qt.Key.Key_8, "", ctrl())) == b"\x7f"  # DEL


def test_alt_letter_is_esc_char() -> None:
    assert encode_key(key(Qt.Key.Key_A, "a", ALT)) == b"\x1ba"


# -- Cursor keys: CSI, SS3 (DECCKM), and modifier encodings --------------


def test_arrows_plain_csi() -> None:
    assert encode_key(key(Qt.Key.Key_Up, "")) == b"\x1b[A"
    assert encode_key(key(Qt.Key.Key_Down, "")) == b"\x1b[B"
    assert encode_key(key(Qt.Key.Key_Right, "")) == b"\x1b[C"
    assert encode_key(key(Qt.Key.Key_Left, "")) == b"\x1b[D"


def test_arrows_decckm_ss3() -> None:
    assert encode_key(key(Qt.Key.Key_Up, ""), dec_ckm=True) == b"\x1bOA"
    assert encode_key(key(Qt.Key.Key_Down, ""), dec_ckm=True) == b"\x1bOB"
    assert encode_key(key(Qt.Key.Key_Right, ""), dec_ckm=True) == b"\x1bOC"
    assert encode_key(key(Qt.Key.Key_Left, ""), dec_ckm=True) == b"\x1bOD"


def test_arrow_single_modifiers() -> None:
    assert encode_key(key(Qt.Key.Key_Up, "", SHIFT)) == b"\x1b[1;2A"
    assert encode_key(key(Qt.Key.Key_Up, "", ALT)) == b"\x1b[1;3A"
    assert encode_key(key(Qt.Key.Key_Up, "", ctrl())) == b"\x1b[1;5A"


def test_arrow_combined_modifiers() -> None:
    assert encode_key(key(Qt.Key.Key_Up, "", ctrl() | SHIFT)) == b"\x1b[1;6A"
    assert encode_key(key(Qt.Key.Key_Up, "", ctrl() | ALT)) == b"\x1b[1;7A"


def test_home_end_plain_and_modifier() -> None:
    assert encode_key(key(Qt.Key.Key_Home, "")) == b"\x1b[H"
    assert encode_key(key(Qt.Key.Key_End, "")) == b"\x1b[F"
    assert encode_key(key(Qt.Key.Key_Home, "", ctrl())) == b"\x1b[1;5H"


# -- Page keys: viewport policy (spec Q8) --------------------------------


def test_pgup_scrolls_viewport_when_history_exists() -> None:
    assert encode_key(key(Qt.Key.Key_PageUp, ""), scrollback_len=5) is None


def test_pgup_sends_csi_when_no_history() -> None:
    assert encode_key(key(Qt.Key.Key_PageUp, ""), scrollback_len=0) == b"\x1b[5~"
    assert encode_key(key(Qt.Key.Key_PageDown, ""), scrollback_len=0) == b"\x1b[6~"


def test_pgdn_scrolls_viewport_when_history_exists() -> None:
    assert encode_key(key(Qt.Key.Key_PageDown, ""), scrollback_len=3) is None


# -- Paste and copy -------------------------------------------------------


def test_paste_shortcuts_return_none() -> None:
    assert encode_key(key(Qt.Key.Key_V, "\x16", ctrl() | SHIFT)) is None
    assert encode_key(key(Qt.Key.Key_Insert, "", SHIFT)) is None


def test_copy_shortcut_is_noop() -> None:
    assert encode_key(key(Qt.Key.Key_C, "\x03", ctrl() | SHIFT)) is None


def test_encode_paste_plain_and_bracketed() -> None:
    assert encode_paste("hi") == b"hi"
    assert encode_paste("héllo") == "héllo".encode()
    assert encode_paste("hi", bracketed_paste=True) == b"\x1b[200~hi\x1b[201~"


# -- macOS: Command never reaches the terminal -----


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS ⌘ semantics")
def test_command_key_never_reaches_the_terminal() -> None:
    # Qt reports ⌘ as ControlModifier on macOS — it must never become a
    # control code (⌘+C is copy, not SIGINT).
    assert (
        encode_key(key(Qt.Key.Key_C, "\x03", Qt.KeyboardModifier.ControlModifier))
        is None
    )
    assert encode_key(key(Qt.Key.Key_V, "", Qt.KeyboardModifier.ControlModifier)) is None
    assert (
        encode_key(key(Qt.Key.Key_Up, "", Qt.KeyboardModifier.ControlModifier)) is None
    )


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS ⌃ semantics")
def test_control_key_is_meta_modifier_on_macos() -> None:
    # ⌃ is the real Ctrl on macOS (Qt: MetaModifier) → control codes.
    assert encode_key(key(Qt.Key.Key_C, "\x03", Qt.KeyboardModifier.MetaModifier)) == b"\x03"
    assert (
        encode_key(key(Qt.Key.Key_Up, "", Qt.KeyboardModifier.MetaModifier))
        == b"\x1b[1;5A"
    )


# -- Mouse protocol encoders (DECSET ?1000 X10 / ?1006 SGR) --------------


def test_x10_mouse_press_encodes_32_offset_bytes() -> None:
    # CSI M + three bytes, each +32: button, column, row.
    assert encode_mouse_x10(3, 5, 0, action="press") == b"\x1b[M" + bytes((32, 35, 37))


def test_x10_mouse_buttons_and_release() -> None:
    assert encode_mouse_x10(1, 1, 1, action="press") == b"\x1b[M!!!"  # middle
    assert encode_mouse_x10(1, 1, 2, action="press") == b"\x1b[M\"!!"  # right
    assert encode_mouse_x10(2, 2, 0, action="release") == b"\x1b[M" + bytes((35, 34, 34))


def test_x10_mouse_motion_adds_32() -> None:
    assert encode_mouse_x10(2, 2, 0, action="motion") == b"\x1b[M" + bytes((64, 34, 34))


def test_x10_mouse_modifier_bits() -> None:
    # shift 4, meta/alt 8, ctrl 16 — summed into the button byte.
    assert encode_mouse_x10(1, 1, 0, action="press", mods=4) == b"\x1b[M" + bytes((36, 33, 33))
    assert encode_mouse_x10(1, 1, 0, action="press", mods=28) == b"\x1b[M" + bytes((60, 33, 33))


def test_x10_wheel_uses_buttons_4_and_5() -> None:
    # The X11 wheel convention xterm reports in legacy modes.
    assert encode_mouse_x10(1, 1, 0, action="wheel_up") == b"\x1b[M" + bytes((36, 33, 33))
    assert encode_mouse_x10(1, 1, 0, action="wheel_down") == b"\x1b[M" + bytes((37, 33, 33))


def test_x10_wheel_uses_shifted_modifier_bits() -> None:
    # Wheel buttons 4/5 leave no room for the standard modifier bits —
    # the xterm mapping: shift→2, ctrl→4, meta→8.
    assert encode_mouse_x10(1, 1, 0, action="wheel_up", mods=4) == b"\x1b[M" + bytes((38, 33, 33))
    assert encode_mouse_x10(1, 1, 0, action="wheel_down", mods=16) == b"\x1b[M" + bytes((41, 33, 33))
    assert encode_mouse_x10(1, 1, 0, action="wheel_up", mods=8) == b"\x1b[M" + bytes((44, 33, 33))


def test_sgr_mouse_press_left() -> None:
    assert encode_sgr_mouse(4, 7, 0, action="press") == b"\x1b[<0;4;7M"


def test_sgr_mouse_release_uses_lowercase_m() -> None:
    assert encode_sgr_mouse(4, 7, 2, action="release") == b"\x1b[<2;4;7m"


def test_sgr_mouse_motion_adds_32() -> None:
    assert encode_sgr_mouse(4, 7, 0, action="motion") == b"\x1b[<32;4;7M"


def test_sgr_mouse_wheel_up_down() -> None:
    assert encode_sgr_mouse(4, 7, 0, action="wheel_up") == b"\x1b[<64;4;7M"
    assert encode_sgr_mouse(4, 7, 0, action="wheel_down") == b"\x1b[<65;4;7M"


def test_sgr_mouse_wheel_uses_shifted_modifier_bits() -> None:
    # Shifted wheel is 66/67 — the standard shift bit (4) would collide
    # with the button code; the xterm mapping: shift→2, ctrl→4, meta→8.
    assert encode_sgr_mouse(4, 7, 0, action="wheel_up", mods=4) == b"\x1b[<66;4;7M"
    assert encode_sgr_mouse(4, 7, 0, action="wheel_down", mods=4) == b"\x1b[<67;4;7M"
    assert encode_sgr_mouse(4, 7, 0, action="wheel_up", mods=16) == b"\x1b[<68;4;7M"
    assert encode_sgr_mouse(4, 7, 0, action="wheel_up", mods=4 | 16) == b"\x1b[<70;4;7M"


def test_sgr_mouse_modifiers_sum_into_button() -> None:
    assert encode_sgr_mouse(4, 7, 0, action="press", mods=4) == b"\x1b[<4;4;7M"
    assert encode_sgr_mouse(4, 7, 0, action="press", mods=4 | 8 | 16) == b"\x1b[<28;4;7M"
    assert encode_sgr_mouse(4, 7, 0, action="motion", mods=16) == b"\x1b[<48;4;7M"
