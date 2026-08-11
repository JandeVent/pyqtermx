# -*- coding: utf-8 -*-
# Copyright (C) 2018-2026 Connet Information Technology Company, Shanghai.
"""OSC 4/10/11/12 color queries — the terminal answers the child's
theme detection queries and applies its cursor color (Phase 5).

TUI apps (opencode, vim, …) query the palette (`OSC 4`), the default
foreground (`OSC 10`), and the default background (`OSC 11`) to decide
their light/dark theme; the emulator replies from `pyqtermx.palette`
with the xterm 16-bit `rgb:RRRR/GGGG/BBBB` form. Set forms
parse-and-ignore (palette mutation is a follow-up); `OSC 12` (cursor
color) and `OSC 112` (reset) apply — the cursor color is visible
state. Everything else stays a no-op until its step.
"""

from pyqtermx.emulator import Emulator
from pyqtermx.parser import Parser
from pyqtermx.screen import Screen


def make_parser(replies: list[str]) -> Parser:
    """A parser whose emulator appends every OSC reply to `replies`."""
    return Parser(Emulator(Screen(), reply=replies.append))


def emulator(parser: Parser) -> Emulator:
    """The parser's dispatcher — the emulator under test. One
    type-ignore here instead of one at every state-asserting test
    (Parser types `_dispatcher` as the protocol, not the Emulator)."""
    return parser._dispatcher  # type: ignore[attr-defined]


def feed(parser: Parser, data: bytes) -> None:
    """Feed bytes in pty-sized chunks — the chunking invariant (T6):
    byte-wise feeding must equal one big feed, replies included."""
    for byte in data:
        parser.feed_bytes(bytes([byte]))


# -- OSC 4 — palette queries --------------------------------------------


def test_osc_4_query_all_16_colors() -> None:
    replies: list[str] = []
    feed(make_parser(replies), b"\x1b]4;?\x07")
    assert len(replies) == 1
    reply = replies[0]
    assert reply.startswith("\x1b]4;")
    assert reply.endswith("\x07")
    entries = reply[2:-1].split(";")  # "4", "0", "rgb:…", …
    assert entries[0] == "4"
    assert len(entries) == 1 + 16 * 2
    pairs = dict(zip(entries[1::2], entries[2::2]))
    assert pairs["0"] == "rgb:0000/0000/0000"  # ANSI black
    assert pairs["1"] == "rgb:cdcd/0000/0000"  # ANSI red (#CD0000)
    assert pairs["7"] == "rgb:e5e5/e5e5/e5e5"  # ANSI white
    assert pairs["15"] == "rgb:ffff/ffff/ffff"  # ANSI bright white


def test_osc_4_query_single_index() -> None:
    replies: list[str] = []
    feed(make_parser(replies), b"\x1b]4;1;?\x07")
    assert replies == ["\x1b]4;1;rgb:cdcd/0000/0000\x07"]


def test_osc_4_query_multiple_indices() -> None:
    replies: list[str] = []
    feed(make_parser(replies), b"\x1b]4;1;?;2;?\x07")
    assert replies == ["\x1b]4;1;rgb:cdcd/0000/0000;2;rgb:0000/cdcd/0000\x07"]


def test_osc_4_query_grouped_indices() -> None:
    """`4;0;1;?` — indices grouped before a single `?` (a form some
    apps send) — replies both."""
    replies: list[str] = []
    feed(make_parser(replies), b"\x1b]4;0;1;?\x07")
    assert replies == ["\x1b]4;0;rgb:0000/0000/0000;1;rgb:cdcd/0000/0000\x07"]


def test_osc_4_query_256_color_indices() -> None:
    replies: list[str] = []
    feed(make_parser(replies), b"\x1b]4;196;?;232;?\x07")
    # 196 → cube r=255; 232 → grayscale 8.
    assert replies == [
        "\x1b]4;196;rgb:ffff/0000/0000;232;rgb:0808/0808/0808\x07"
    ]


def test_osc_4_set_form_is_ignored() -> None:
    replies: list[str] = []
    feed(make_parser(replies), b"\x1b]4;1;#ff0000\x07\x1b]4;2;rgb:0000/ff00/0000\x07")
    assert replies == []


def test_osc_4_out_of_range_index_is_skipped() -> None:
    replies: list[str] = []
    feed(make_parser(replies), b"\x1b]4;300;?;1;?\x07")
    assert replies == ["\x1b]4;1;rgb:cdcd/0000/0000\x07"]


# -- OSC 10/11 — fg/bg queries -------------------------------------------


def test_osc_10_and_11_query_defaults() -> None:
    replies: list[str] = []
    parser = make_parser(replies)
    feed(parser, b"\x1b]10;?\x07\x1b]11;?\x07")
    assert replies == ["\x1b]10;rgb:e8e8/e8e8/e8e8\x07", "\x1b]11;rgb:1010/1010/1010\x07"]


def test_osc_10_and_11_set_forms_are_ignored() -> None:
    replies: list[str] = []
    feed(make_parser(replies), b"\x1b]10;#ffffff\x07\x1b]11;rgb:ffff/ffff/ffff\x07")
    assert replies == []


def test_set_palette_updates_queries() -> None:
    replies: list[str] = []
    parser = make_parser(replies)
    emulator(parser).set_palette("#ffffff", "#000000")
    feed(parser, b"\x1b]10;?\x07\x1b]11;?\x07")
    assert replies == ["\x1b]10;rgb:ffff/ffff/ffff\x07", "\x1b]11;rgb:0000/0000/0000\x07"]


# -- OSC 12 — cursor color ---------------------------------------------


def test_osc_12_set_form_sets_cursor_color() -> None:
    parser = make_parser([])
    feed(parser, b"\x1b]12;#1a1a1a\x07")
    assert emulator(parser).cursor_color == "#1a1a1a"


def test_osc_12_rgb_form_sets_cursor_color() -> None:
    parser = make_parser([])
    feed(parser, b"\x1b]12;rgb:1a1a/1a1a/1a1a\x07")
    assert emulator(parser).cursor_color == "#1a1a1a"


def test_osc_12_query_replies_the_set_color() -> None:
    replies: list[str] = []
    parser = make_parser(replies)
    feed(parser, b"\x1b]12;#1a1a1a\x07\x1b]12;?\x07")
    assert replies == ["\x1b]12;rgb:1a1a/1a1a/1a1a\x07"]


def test_osc_12_query_before_set_is_silent() -> None:
    replies: list[str] = []
    feed(make_parser(replies), b"\x1b]12;?\x07")
    assert replies == []


def test_osc_12_malformed_spec_is_ignored() -> None:
    parser = make_parser([])
    feed(parser, b"\x1b]12;#12345\x07\x1b]12;rgb:zzzz/0000/0000\x07")
    assert emulator(parser).cursor_color is None


def test_osc_12_rgb_form_wrong_arity_is_ignored() -> None:
    parser = make_parser([])
    feed(parser, b"\x1b]12;rgb:ffff/ffff\x07")
    assert emulator(parser).cursor_color is None


def test_osc_112_resets_cursor_color() -> None:
    replies: list[str] = []
    parser = make_parser(replies)
    feed(parser, b"\x1b]12;#1a1a1a\x07\x1b]112\x07\x1b]12;?\x07")
    assert emulator(parser).cursor_color is None
    assert replies == []


# -- Terminators & robustness --------------------------------------------


def test_st_terminator_dispatches_same_reply() -> None:
    """The ECMA-48 form (ESC \\) terminates OSC just like BEL (the
    parser strips both; the reply always uses BEL, xterm style)."""
    replies: list[str] = []
    feed(make_parser(replies), b"\x1b]11;?\x1b\\")
    assert replies == ["\x1b]11;rgb:1010/1010/1010\x07"]


def test_unknown_osc_is_a_noop() -> None:
    replies: list[str] = []
    feed(make_parser(replies), b"\x1b]0;title\x07\x1b]52;c;AQID\x07\x1b]8;;uri\x07")
    assert replies == []


def test_query_without_reply_callback_is_safe() -> None:
    parser = Parser(Emulator(Screen()))
    feed(parser, b"\x1b]4;?;1;?\x07\x1b]11;?\x07")
    # No reply callback — queries silently drop; nothing crashes.
