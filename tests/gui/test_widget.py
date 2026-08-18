# -*- coding: utf-8 -*-
# Copyright (C) 2018-2026 Connet Information Technology Company, Shanghai.
"""T08 — the widget end-to-end (offscreen): snapshots → pixels, keys →
bytes, viewport scrolling, paste, resize debounce. The GUI never reads
the model — assertions use the fake pty's `sent` bytes and winsizes,
the widget's backing image, and (for viewport position only) the
session screen, as the harness does elsewhere.

The session runs a real reader thread; qtbot.waitUntil processes Qt
events so queued snapshots get applied.
"""

from __future__ import annotations

import math
import sys
import time
from collections.abc import Iterator

import pytest
from pytestqt.qtbot import QtBot
from PyQt6.QtCore import QEvent, QPoint, QPointF, QRect, QSize, Qt
from PyQt6.QtGui import (
    QClipboard,
    QColor,
    QFocusEvent,
    QInputMethodEvent,
    QKeyEvent,
    QMouseEvent,
    QPaintEvent,
    QWheelEvent,
)
from PyQt6.QtWidgets import QApplication

from pyqtermx.render import DEFAULT_BG, DEFAULT_FG
from pyqtermx.screen import Cell, Row, rgb
from pyqtermx.selection import Selection
from pyqtermx.session import Session, Snapshot
from pyqtermx.widget import TerminalWidget, merge_viewport

from tests.session.test_session import FakePty, make_session


def press(
    widget: TerminalWidget,
    key: Qt.Key,
    modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier,
    text: str = "",
) -> None:
    """Send a synthetic key press with explicit text (PyQt6's QTest
    helpers don't carry text)."""
    event = QKeyEvent(QEvent.Type.KeyPress, key, modifiers, text)
    QApplication.sendEvent(widget, event)


def ctrl_mod() -> Qt.KeyboardModifier:
    """The modifier Qt reports for the physical Ctrl key: ⌃ (Meta) on
    macOS, Control elsewhere."""
    return (
        Qt.KeyboardModifier.MetaModifier
        if sys.platform == "darwin"
        else Qt.KeyboardModifier.ControlModifier
    )


def wheel(widget: TerminalWidget, delta: int) -> None:
    """Send a synthetic wheel event with the given angle delta."""
    pos = QPointF(widget.rect().center())
    event = QWheelEvent(
        pos,
        pos,
        QPoint(0, 0),
        QPoint(0, delta),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(widget, event)


def clipboard() -> QClipboard:
    """The app clipboard (non-None under QApplication)."""
    cb = QApplication.clipboard()
    assert cb is not None
    return cb


@pytest.fixture
def fake() -> FakePty:
    return FakePty()


@pytest.fixture
def session(fake: FakePty) -> Iterator[Session]:
    s = make_session(fake, lines=5, columns=10)
    yield s
    s.close()  # stop the reader thread before the widget dies


@pytest.fixture
def widget(session: Session, qtbot: QtBot) -> Iterator[TerminalWidget]:
    w = TerminalWidget(session)
    qtbot.addWidget(w)
    yield w


def test_initial_snapshot_paints_the_viewport(widget: TerminalWidget, session: Session) -> None:
    # set_session re-applies the initial full snapshot: the backing image
    # is 5×10 cells of default background.
    assert widget._image.size().width() == round(10 * widget._renderer.cell_w)
    assert widget._image.size().height() == 5 * widget._renderer.cell_h


def test_backing_image_is_dpr_scaled(widget: TerminalWidget) -> None:
    """The backing store is rendered at the widget's device-pixel
    ratio — the contract that keeps the blit 1:1 physical pixels (a 1×
    raster upscaled to a 2× Retina surface is blurry). Offscreen the
    ratio is 1, but the invariants pin the structure: image pixels =
    logical × dpr, image DPR = widget DPR, and sizeHint stays
    logical so layout never sees the scaled pixels."""
    dpr = widget.devicePixelRatioF()
    assert widget._image.devicePixelRatio() == dpr
    assert widget._image.width() == round(10 * widget._renderer.cell_w * dpr)
    assert widget._image.height() == round(5 * widget._renderer.cell_h * dpr)
    assert widget.sizeHint() == QSize(
        round(widget._image.width() / dpr), round(widget._image.height() / dpr)
    )


def test_output_repaints_pixels(
    widget: TerminalWidget, session: Session, fake: FakePty, qtbot: QtBot
) -> None:
    fake.output(b"hi\n")

    def fg_appeared() -> bool:
        # Row 0 painted (fg pixels present somewhere in the image).
        return any(
            widget._image.pixelColor(x, y) == DEFAULT_FG
            for y in range(widget._renderer.cell_h)
            for x in range(round(widget._renderer.cell_w))
        )

    # waitUntil spins the Qt event loop, delivering the queued snapshot
    # signal to the widget (the sleep loop above would never apply it).
    qtbot.waitUntil(fg_appeared, timeout=5000)


def test_typing_sends_bytes(
    widget: TerminalWidget, session: Session, fake: FakePty, qtbot: QtBot
) -> None:
    press(widget, Qt.Key.Key_A, text="a")
    qtbot.waitUntil(lambda: fake.sent == b"a")
    press(widget, Qt.Key.Key_Return, text="\r")
    qtbot.waitUntil(lambda: fake.sent == b"a\r")


def test_ctrl_c_without_text_sends_intr(
    widget: TerminalWidget, session: Session, fake: FakePty, qtbot: QtBot
) -> None:
    """Live ⌃+C events often carry no text (macOS) — the control code
    must come from the key, or the child never gets its SIGINT (htop)."""
    press(widget, Qt.Key.Key_C, ctrl_mod(), text="")
    qtbot.waitUntil(lambda: fake.sent == b"\x03")
    assert fake.sent == b"\x03"  # VINTR, and nothing else


def test_shift_tab_sends_backtab(
    widget: TerminalWidget, session: Session, fake: FakePty, qtbot: QtBot
) -> None:
    """Shift+Tab (Key_Backtab, text-less) reaches the child as CSI Z."""
    press(widget, Qt.Key.Key_Backtab, text="")
    qtbot.waitUntil(lambda: fake.sent == b"\x1b[Z")
    assert fake.sent == b"\x1b[Z"


def test_ime_commit_sends_utf8(
    widget: TerminalWidget, session: Session, fake: FakePty, qtbot: QtBot
) -> None:
    """IME committed text (Chinese etc.) reaches the child as UTF-8."""
    event = QInputMethodEvent()
    event.setCommitString("\u4f60\u597d")  # 你好
    QApplication.sendEvent(widget, event)
    qtbot.waitUntil(lambda: fake.sent == "\u4f60\u597d".encode("utf-8"))


def test_ime_preedit_sends_nothing(
    widget: TerminalWidget, session: Session, fake: FakePty, qtbot: QtBot
) -> None:
    """Composition keystrokes (preedit) are accepted but not forwarded —
    the terminal has no composition preview."""
    event = QInputMethodEvent("ni", [])  # preedit only, no commit
    QApplication.sendEvent(widget, event)
    qtbot.waitUntil(lambda: fake.sent == b"")
    assert fake.sent == b""
    assert event.isAccepted()


def test_ime_cursor_rect_anchors_to_cursor(
    widget: TerminalWidget, session: Session, fake: FakePty, qtbot: QtBot
) -> None:
    """The IME candidate window anchors to the cursor cell (widget
    coordinates) — not a default position below the window."""
    fake.output(b"ab")  # cursor at col 2, row 0
    qtbot.waitUntil(lambda: widget._last_snapshot is not None)
    qtbot.waitUntil(lambda: widget._last_snapshot.cursor == (0, 2))

    rect = widget.inputMethodQuery(Qt.InputMethodQuery.ImCursorRectangle)
    assert rect == QRect(
        round(2 * widget._renderer.cell_w),
        0 * widget._renderer.cell_h,
        round(widget._renderer.cell_w),
        widget._renderer.cell_h,
    )
    assert widget.inputMethodQuery(Qt.InputMethodQuery.ImEnabled) is True


def test_ime_cursor_rect_follows_scrolled_viewport(
    widget: TerminalWidget, session: Session, qtbot: QtBot
) -> None:
    """Scrolled up (viewport_offset > 0): the anchor rides the cursor's
    viewport row (grid row + offset), not the grid row."""
    rows = tuple(Row([Cell.blank()] * widget._columns) for _ in range(widget._lines))
    snap = Snapshot(
        dirty_rows=(),
        rows=rows,
        scrollback_len=widget._lines,
        viewport_offset=2,
        cursor=(0, 2),
        full=True,
    )
    widget._apply_snapshot(snap)
    rect = widget.inputMethodQuery(Qt.InputMethodQuery.ImCursorRectangle)
    assert rect == QRect(
        round(2 * widget._renderer.cell_w),
        (0 + 2) * widget._renderer.cell_h,
        round(widget._renderer.cell_w),
        widget._renderer.cell_h,
    )


def test_arrow_sends_csi_without_decckm(
    widget: TerminalWidget, session: Session, fake: FakePty, qtbot: QtBot
) -> None:
    press(widget, Qt.Key.Key_Up)
    qtbot.waitUntil(lambda: fake.sent == b"\x1b[A")


def test_decckm_arrow_sends_ss3(
    widget: TerminalWidget, session: Session, fake: FakePty, qtbot: QtBot
) -> None:
    fake.output(b"\x1b[?1h")  # DECCKM on
    qtbot.waitUntil(lambda: widget._dec_ckm)
    press(widget, Qt.Key.Key_Up)
    qtbot.waitUntil(lambda: fake.sent == b"\x1bOA")


def test_ctrl_shift_v_pastes(
    widget: TerminalWidget, session: Session, fake: FakePty, qtbot: QtBot
) -> None:
    clipboard().setText("hello")
    press(widget, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier, "\x16")
    qtbot.waitUntil(lambda: fake.sent == b"hello")


def test_paste_is_bracketed_when_requested(
    widget: TerminalWidget, session: Session, fake: FakePty, qtbot: QtBot
) -> None:
    fake.output(b"\x1b[?2004h")  # bracketed paste on
    qtbot.waitUntil(lambda: widget._bracketed_paste)
    clipboard().setText("hi")
    press(widget, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier, "\x16")
    qtbot.waitUntil(lambda: fake.sent == b"\x1b[200~hi\x1b[201~")


def test_shift_insert_pastes(
    widget: TerminalWidget, session: Session, fake: FakePty, qtbot: QtBot
) -> None:
    clipboard().setText("x")
    press(widget, Qt.Key.Key_Insert, Qt.KeyboardModifier.ShiftModifier)
    qtbot.waitUntil(lambda: fake.sent == b"x")


def test_ctrl_shift_c_sends_nothing(
    widget: TerminalWidget, session: Session, fake: FakePty, qtbot: QtBot
) -> None:
    press(widget, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier, "\x03")
    qtbot.waitUntil(lambda: not fake.sent)
    assert fake.sent == b""


def test_pgup_scrolls_viewport_when_history_exists(
    widget: TerminalWidget, session: Session, fake: FakePty, qtbot: QtBot
) -> None:
    for i in range(30):
        fake.output(f"line {i}\r\n".encode())
    # Wait on the widget's mirror, not the model — the snapshot that
    # carries the scrollback length must have been applied.
    qtbot.waitUntil(lambda: widget._scrollback_len > 0)
    press(widget, Qt.Key.Key_PageUp)
    qtbot.waitUntil(lambda: session.screen.viewport_offset == 5)
    assert fake.sent == b""  # never reached the child


def test_pgdn_returns_to_live_output(
    widget: TerminalWidget, session: Session, fake: FakePty, qtbot: QtBot
) -> None:
    for i in range(30):
        fake.output(f"line {i}\r\n".encode())
    # Wait on the widget's mirror, not the model — the snapshot that
    # carries the scrollback length must have been applied.
    qtbot.waitUntil(lambda: widget._scrollback_len > 0)
    press(widget, Qt.Key.Key_PageUp)
    qtbot.waitUntil(lambda: session.screen.viewport_offset == 5)
    press(widget, Qt.Key.Key_PageDown)
    qtbot.waitUntil(lambda: session.screen.viewport_offset == 0)


def test_wheel_scrolls_viewport(
    widget: TerminalWidget, session: Session, fake: FakePty, qtbot: QtBot
) -> None:
    for i in range(30):
        fake.output(f"line {i}\r\n".encode())
    # Wait on the widget's mirror, not the model — the snapshot that
    # carries the scrollback length must have been applied.
    qtbot.waitUntil(lambda: widget._scrollback_len > 0)
    wheel(widget, 120)  # scroll up
    qtbot.waitUntil(lambda: session.screen.viewport_offset == 3)


def test_wheel_banks_subnotch_trackpad_deltas(
    widget: TerminalWidget, session: Session, fake: FakePty, qtbot: QtBot
) -> None:
    # A trackpad swipe delivers many small deltas; four 30° ticks bank
    # into one 120° notch = WHEEL_ROWS rows, not four (the alt-screen
    # path banks the same way — the main screen must too, or a swipe
    # scrolls 3 rows per event instead of per notch).
    for i in range(30):
        fake.output(f"line {i}\r\n".encode())
    qtbot.waitUntil(lambda: widget._scrollback_len > 0)
    for _ in range(4):
        wheel(widget, 30)
    qtbot.waitUntil(lambda: session.screen.viewport_offset == 3)


def test_wheel_subnotch_delta_does_not_scroll(
    widget: TerminalWidget, session: Session, fake: FakePty, qtbot: QtBot
) -> None:
    # A single sub-notch tick (trackpad micro-event) banks but does not
    # scroll — the viewport moves only when a full 120° notch accrues.
    for i in range(30):
        fake.output(f"line {i}\r\n".encode())
    qtbot.waitUntil(lambda: widget._scrollback_len > 0)
    wheel(widget, 30)
    qtbot.wait(50)
    assert session.screen.viewport_offset == 0


def test_wheel_scrolls_one_row_per_40_degrees(
    widget: TerminalWidget, session: Session, fake: FakePty, qtbot: QtBot
) -> None:
    # The smooth-scroll granularity: a trackpad tick of 40° scrolls one
    # row (120/WHEEL_ROWS per row), so a swipe steps row by row instead
    # of jumping WHEEL_ROWS rows per event.
    for i in range(30):
        fake.output(f"line {i}\r\n".encode())
    qtbot.waitUntil(lambda: widget._scrollback_len > 0)
    wheel(widget, 40)
    qtbot.waitUntil(lambda: session.screen.viewport_offset == 1)


def test_resize_debounces_pty_winsize(
    widget: TerminalWidget, session: Session, fake: FakePty, qtbot: QtBot
) -> None:
    widget.show()  # resizeEvent only fires on shown widgets
    # The scrollbar extent is always reserved (hidden or not) — the grid
    # width is (widget width − extent) / cell_w. ceil() so the widget is
    # wide enough for exactly 2 cells (round() can fall short of 2·cell_w).
    widget.resize(
        math.ceil(2 * widget._renderer.cell_w) + widget._scrollbar.sizeHint().width(),
        3 * widget._renderer.cell_h,
    )
    qtbot.waitUntil(lambda: bool(fake.winsizes))
    assert fake.winsizes[-1] == (3, 2)


def test_scrollbar_sits_at_the_right_edge(
    widget: TerminalWidget, session: Session, fake: FakePty, qtbot: QtBot
) -> None:
    for i in range(30):
        fake.output(f"line {i}\r\n".encode())
    qtbot.waitUntil(lambda: widget._scrollback_len > 0)
    widget.show()
    widget.resize(500, 300)
    qtbot.waitUntil(lambda: widget._scrollbar.isVisible())
    sb = widget._scrollbar
    extent = sb.sizeHint().width()
    assert sb.geometry().left() == widget.width() - extent
    assert sb.geometry().width() == extent
    assert sb.geometry().height() == widget.height()


def test_scrollbar_appearance_never_clips_the_grid(
    widget: TerminalWidget, session: Session, fake: FakePty, qtbot: QtBot
) -> None:
    widget.show()
    extent = widget._scrollbar.sizeHint().width()
    widget.resize(math.ceil(3 * widget._renderer.cell_w) + extent, 5 * widget._renderer.cell_h)
    qtbot.waitUntil(lambda: bool(fake.winsizes))
    assert fake.winsizes[-1] == (5, 3)
    # History appears → the scrollbar shows — but no resize is posted:
    # the extent was reserved from the start, so nothing reflows/clips.
    for i in range(30):
        fake.output(f"line {i}\r\n".encode())
    qtbot.waitUntil(lambda: widget._scrollback_len > 0)
    qtbot.waitUntil(lambda: widget._scrollbar.isVisible())
    assert fake.winsizes[-1] == (5, 3)


def test_scrollbar_drag_does_not_compound_deltas(
    widget: TerminalWidget, session: Session, fake: FakePty, qtbot: QtBot
) -> None:
    # A drag fires many valueChanged events faster than the session
    # round-trips; each delta must be relative to the last requested
    # position, not the last snapshot's offset, or the deltas compound
    # and the viewport overshoots the handle.
    for i in range(30):
        fake.output(f"line {i}\r\n".encode())
    # 30 lines on a 5-row screen: 25 scroll off, the trailing CRLF of the
    # last line scrolls once more → 26 rows of history.
    qtbot.waitUntil(lambda: widget._scrollback_len == 26)
    widget._on_scrollbar(16)  # drag up: value 26 → 16
    widget._on_scrollbar(6)  # ... → 6, before any snapshot lands
    qtbot.waitUntil(lambda: session.screen.viewport_offset == 20, timeout=5000)


def test_scrollbar_handle_not_snapped_while_dragging(
    widget: TerminalWidget, session: Session, fake: FakePty, qtbot: QtBot
) -> None:
    # While the handle is being dragged, a snapshot must not yank it
    # back to the last processed offset — the handle follows the mouse
    # and the session catches up in the background.
    for i in range(30):
        fake.output(f"line {i}\r\n".encode())
    # 30 lines on a 5-row screen: 25 scroll off, the trailing CRLF of the
    # last line scrolls once more → 26 rows of history.
    qtbot.waitUntil(lambda: widget._scrollback_len == 26)
    sb = widget._scrollbar
    sb.setSliderDown(True)  # the user is dragging the handle
    sb.setValue(16)  # the handle is where the mouse is
    widget._update_scrollbar()  # a snapshot arrives mid-drag
    assert sb.value() == 16  # not yanked back to 26 (scrollback_len - offset)


def test_paint_fills_the_area_beyond_the_grid(
    widget: TerminalWidget, session: Session, qtbot: QtBot
) -> None:
    widget.show()
    # A size that is not a multiple of the cell grid → a sliver exists at
    # the bottom/right — it must be the terminal background, not the
    # palette's window color.
    widget.resize(
        round(2 * widget._renderer.cell_w) + widget._scrollbar.sizeHint().width() + 7,
        2 * widget._renderer.cell_h + 5,
    )
    qtbot.waitUntil(lambda: widget._image.size().width() == round(2 * widget._renderer.cell_w))
    img = widget.grab().toImage()
    assert img.pixelColor(img.width() - 1, img.height() - 1) == DEFAULT_BG


def test_resize_paints_last_state_no_stale_frame(
    widget: TerminalWidget, session: Session, fake: FakePty, qtbot: QtBot
) -> None:
    fake.output(b"hello")
    qtbot.waitUntil(
        lambda: any(
            widget._image.pixelColor(x, y) == DEFAULT_FG
            for y in range(widget._renderer.cell_h)
            for x in range(round(widget._renderer.cell_w))
        )
    )
    widget.show()
    widget.resize(
        math.ceil(4 * widget._renderer.cell_w) + widget._scrollbar.sizeHint().width(),
        5 * widget._renderer.cell_h,
    )
    qtbot.waitUntil(lambda: widget._image.size().width() == round(4 * widget._renderer.cell_w))
    # The fresh image is repainted from the last snapshot — content is
    # visible immediately, not black until the next full snapshot.
    assert any(
        widget._image.pixelColor(x, y) == DEFAULT_FG
        for y in range(widget._renderer.cell_h)
        for x in range(round(widget._renderer.cell_w))
    )


def test_tab_is_not_swallowed_by_focus_navigation(
    widget: TerminalWidget, session: Session, fake: FakePty, qtbot: QtBot
) -> None:
    # focusNextPrevChild is disabled: Tab must reach the shell, not move
    # focus.
    assert widget.focusNextPrevChild(True) is False
    press(widget, Qt.Key.Key_Tab, text="\t")
    qtbot.waitUntil(lambda: fake.sent == b"\t")


# -- partial rendering: update() covers only the damaged region ----------


def _blank_rows(session: Session) -> tuple:
    from pyqtermx.screen import Cell, Row

    return tuple(Row([Cell.blank() for _ in range(session.columns)]) for _ in range(2))


def test_snapshot_repaint_limited_to_dirty_region(
    widget: TerminalWidget, session: Session
) -> None:
    rects: list[QRect] = []
    widget.update = lambda rect: rects.append(rect)  # type: ignore[method-assign]
    ch = widget._renderer.cell_h

    def snap(dirty: tuple[int, ...], *, full: bool) -> Snapshot:
        return Snapshot(
            dirty_rows=dirty,
            rows=_blank_rows(session) if dirty else (),
            scrollback_len=0,
            viewport_offset=0,
            cursor=(-1, 0),
            full=full,
        )

    # An incremental snapshot with dirty rows 2–3: the paint event covers
    # exactly those two rows, not the whole frame.
    widget._apply_snapshot(snap((2, 3), full=False))
    assert len(rects) == 1
    assert rects[0].top() == 2 * ch
    assert rects[0].height() == 2 * ch
    assert rects[0].width() == widget._image.width()

    # A mode-only snapshot changed nothing visible: no repaint at all.
    widget._apply_snapshot(snap((), full=False))
    assert len(rects) == 1

    # A full snapshot repaints the whole grid.
    widget._apply_snapshot(snap((), full=True))
    assert rects[1] == QRect(0, 0, widget._image.width(), widget._image.height())


# -- full repaint: the backing heals itself ---------------------------------


def row_has_ink(widget: TerminalWidget, row: int) -> bool:
    """Any non-background pixel in the row's cell band. Glyphs must be
    probed by scanning the band — antialiasing blends glyph edges into
    the background, so a single center pixel (or even an exact
    foreground match) is font-dependent: at 12px Menlo 'h' has solid
    foreground pixels but 'H' has none."""
    r = widget._renderer
    return any(
        widget._image.pixelColor(x, y) != DEFAULT_BG
        for y in range(row * r.cell_h, (row + 1) * r.cell_h)
        for x in range(widget._image.width())
    )


def content_rows(widget: TerminalWidget, char: str) -> tuple[Row, ...]:
    """`widget._lines` rows with `char` at cell 0 — viewport content
    for the full-repaint tests."""
    row = Row([Cell(char)] + [Cell.blank()] * (widget._columns - 1))
    return tuple(Row(row.cells) for _ in range(widget._lines))


def full_content_snapshot(widget: TerminalWidget, char: str) -> Snapshot:
    return Snapshot(
        dirty_rows=(),
        rows=content_rows(widget, char),
        scrollback_len=0,
        viewport_offset=0,
        cursor=(-1, 0),
        full=True,
    )


def test_full_paint_rerenders_stale_backing(widget: TerminalWidget) -> None:
    """A full repaint re-renders the backing from the merged viewport —
    a blanked backing (the compositor dropped the surface on display
    sleep/wake) heals itself instead of being blitted as-is."""
    widget._apply_snapshot(full_content_snapshot(widget, "H"))
    widget._image.fill(DEFAULT_BG)  # the surface came back blank
    grid = QRect(0, 0, widget._image.width(), widget._image.height())
    widget.paintEvent(QPaintEvent(grid))
    assert row_has_ink(widget, 0)


def test_partial_paint_blits_without_rerender(widget: TerminalWidget) -> None:
    """A partial repaint blits the backing — no frame re-render: a row
    blanked in the backing stays blank after repainting only that row
    (the heal fires only when the whole grid is damaged)."""
    widget._apply_snapshot(full_content_snapshot(widget, "H"))
    widget._image.fill(DEFAULT_BG)
    row0 = QRect(0, 0, widget._image.width(), widget._renderer.cell_h)
    widget.paintEvent(QPaintEvent(row0))
    assert not row_has_ink(widget, 0)


def test_dpr_change_rebuilds_full_frame(widget: TerminalWidget) -> None:
    """A device-pixel-ratio rebuild re-renders the whole frame from the
    merged viewport — rendering only the last snapshot's dirty rows
    into the fresh backing would blank every other row. (`changeEvent`
    is invoked directly: QWidget only routes DevicePixelRatioChange to
    it when the widget has a window handle, i.e. once shown.)"""
    widget._apply_snapshot(full_content_snapshot(widget, "a"))
    # Narrow the last snapshot to row 0 (as a real write would): the
    # viewport still holds the other rows — they must survive the rebuild.
    widget._apply_snapshot(
        Snapshot(
            dirty_rows=(0,),
            rows=(content_rows(widget, "a")[0],),
            scrollback_len=0,
            viewport_offset=0,
            cursor=(-1, 0),
        )
    )
    widget._image.fill(DEFAULT_BG)  # the fresh backing a rebuild starts from
    widget.changeEvent(QEvent(QEvent.Type.DevicePixelRatioChange))
    assert row_has_ink(widget, 0)  # the dirty row
    assert row_has_ink(widget, 1)  # and the rest of the frame


# -- Mouse: selection, copy, paste, protocol ---------------------------------


def cell_pos(widget: TerminalWidget, row: int, col: int) -> QPoint:
    r = widget._renderer
    return QPoint(round(col * r.cell_w + r.cell_w / 2), row * r.cell_h + r.cell_h // 2)


def mouse_event(
    widget: TerminalWidget,
    etype: QEvent.Type,
    pos: QPoint,
    button: Qt.MouseButton = Qt.MouseButton.NoButton,
    buttons: Qt.MouseButton = Qt.MouseButton.NoButton,
    mods: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier,
) -> None:
    event = QMouseEvent(etype, QPointF(pos), button, buttons, mods)
    QApplication.sendEvent(widget, event)


def mouse_press(
    widget: TerminalWidget,
    pos: QPoint,
    button: Qt.MouseButton = Qt.MouseButton.LeftButton,
    mods: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier,
) -> None:
    mouse_event(widget, QEvent.Type.MouseButtonPress, pos, button, button, mods)


def mouse_release(
    widget: TerminalWidget,
    pos: QPoint,
    button: Qt.MouseButton = Qt.MouseButton.LeftButton,
    mods: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier,
) -> None:
    # A release event carries the released button; nothing is held after.
    mouse_event(widget, QEvent.Type.MouseButtonRelease, pos, button, Qt.MouseButton.NoButton, mods)


def mouse_move(
    widget: TerminalWidget,
    pos: QPoint,
    buttons: Qt.MouseButton = Qt.MouseButton.LeftButton,
    mods: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier,
) -> None:
    mouse_event(widget, QEvent.Type.MouseMove, pos, Qt.MouseButton.NoButton, buttons, mods)


def copy_mods() -> Qt.KeyboardModifier:
    """The copy shortcut: ⌘+C on macOS (Qt reports ⌘ as Control),
    Ctrl+Shift+C elsewhere (Ctrl+C alone is SIGINT)."""
    if sys.platform == "darwin":
        return Qt.KeyboardModifier.ControlModifier
    return Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier


def wait_rows(widget: TerminalWidget, qtbot: QtBot, text: str, row: int = 0) -> None:
    """Wait until the merged viewport row `row` starts with `text`."""

    def shown() -> bool:
        rows = widget._viewport_rows
        if rows is None or row >= len(rows):
            return False
        return "".join(c.data for c in rows[row].cells).rstrip().startswith(text)

    qtbot.waitUntil(shown, timeout=5000)


def wait_content(session: Session, marker: str, timeout: float = 5.0) -> None:
    """Wait until the latest snapshot carries `marker` at row 0, cell 0."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if session.snapshots:
            snap = session.snapshots[-1]
            if snap.rows and snap.rows[0].cells[0].data == marker:
                return
        time.sleep(0.01)
    raise TimeoutError(f"no snapshot with {marker!r} at row 0")


def force_full(session: Session) -> None:
    """A same-size resize: the session flags the next emit as full
    (content snapshots are otherwise incremental)."""
    session.resize(session.lines, session.columns)


def wait_full(session: Session, lines: int, timeout: float = 5.0) -> Snapshot:
    """Wait until a full snapshot with content at row 0 arrives."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if session.snapshots:
            snap = session.snapshots[-1]
            if snap.full and len(snap.rows) == lines and snap.rows[0].cells[0].data != " ":
                return snap
        time.sleep(0.01)
    raise TimeoutError("no full snapshot with content")


def enable_mouse(widget: TerminalWidget, qtbot: QtBot, modes: str) -> None:
    """Feed DECSET mode bytes and wait for the widget to mirror them."""
    fake_output = widget._session.pty.output  # type: ignore[attr-defined]
    fake_output(b"\x1b[?" + modes.encode() + b"h")
    flags = {"1000": "_mouse_1000", "1002": "_mouse_1002", "1003": "_mouse_1003", "1006": "_mouse_1006"}
    wanted = [flags[m] for m in modes.split(";")]

    def mirrored() -> bool:
        return all(getattr(widget, f) for f in wanted)

    qtbot.waitUntil(mirrored, timeout=5000)


def test_click_drag_selects_and_copy_copies(
    widget: TerminalWidget, session: Session, fake: FakePty, qtbot: QtBot
) -> None:
    fake.output(b"hello")
    wait_rows(widget, qtbot, "hello")
    clipboard().setText("")
    mouse_press(widget, cell_pos(widget, 0, 0))
    mouse_move(widget, cell_pos(widget, 0, 3))
    mouse_release(widget, cell_pos(widget, 0, 3))
    press(widget, Qt.Key.Key_C, copy_mods())
    assert clipboard().text() == "hell"


def test_double_click_selects_the_word(
    widget: TerminalWidget, session: Session, fake: FakePty, qtbot: QtBot
) -> None:
    fake.output(b"abc def")
    wait_rows(widget, qtbot, "abc def")
    clipboard().setText("")
    pos = cell_pos(widget, 0, 4)
    mouse_press(widget, pos)
    mouse_release(widget, pos)
    mouse_press(widget, pos)
    mouse_release(widget, pos)
    press(widget, Qt.Key.Key_C, copy_mods())
    assert clipboard().text() == "def"


def test_double_click_event_selects_the_word(
    widget: TerminalWidget, session: Session, fake: FakePty, qtbot: QtBot
) -> None:
    # Qt delivers the second press of a double click as a dbl-click
    # event (production path — not just the counting simulation).
    fake.output(b"abc def")
    wait_rows(widget, qtbot, "abc def")
    clipboard().setText("")
    pos = cell_pos(widget, 0, 4)
    mouse_event(widget, QEvent.Type.MouseButtonDblClick, pos, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
    press(widget, Qt.Key.Key_C, copy_mods())
    assert clipboard().text() == "def"


def test_triple_click_selects_the_line(
    widget: TerminalWidget, session: Session, fake: FakePty, qtbot: QtBot
) -> None:
    fake.output(b"hello")
    wait_rows(widget, qtbot, "hello")
    clipboard().setText("")
    pos = cell_pos(widget, 0, 1)
    for _ in range(3):
        mouse_press(widget, pos)
        mouse_release(widget, pos)
    press(widget, Qt.Key.Key_C, copy_mods())
    assert clipboard().text() == "hello"


def test_click_inside_selection_cancels_it(
    widget: TerminalWidget, session: Session, fake: FakePty, qtbot: QtBot
) -> None:
    """A single click — even inside a selection — cancels it: selection
    is drag-driven, so a click never keeps it."""
    fake.output(b"hello")
    wait_rows(widget, qtbot, "hello")
    clipboard().setText("")
    mouse_press(widget, cell_pos(widget, 0, 0))
    mouse_move(widget, cell_pos(widget, 0, 4))
    mouse_release(widget, cell_pos(widget, 0, 4))
    assert widget._selection is not None
    mouse_press(widget, cell_pos(widget, 0, 2))  # inside: cancelled
    mouse_release(widget, cell_pos(widget, 0, 2))
    assert widget._selection is None
    press(widget, Qt.Key.Key_C, copy_mods())
    assert clipboard().text() == ""


def test_bare_click_selects_nothing(
    widget: TerminalWidget, session: Session, fake: FakePty, qtbot: QtBot
) -> None:
    """A bare click (press + release, no drag) creates no selection —
    not even a single cell — and copy is a noop."""
    fake.output(b"hello worl")
    wait_rows(widget, qtbot, "hello worl")
    clipboard().setText("")
    pos = cell_pos(widget, 0, 6)
    mouse_press(widget, pos)
    mouse_release(widget, pos)
    assert widget._selection is None
    press(widget, Qt.Key.Key_C, copy_mods())
    assert clipboard().text() == ""


def test_click_at_drag_end_is_a_fresh_click(
    widget: TerminalWidget, session: Session, fake: FakePty, qtbot: QtBot
) -> None:
    """A click at the *release* cell of a drag must be a fresh click,
    not click #2 — the click counter anchors on press positions, so a
    backwards drag ending at (0,2) must not turn a click there into a
    word selection. A fresh click cancels the selection."""
    fake.output(b"hello worl")
    wait_rows(widget, qtbot, "hello worl")
    clipboard().setText("")
    # back-to-front drag, ending at col 2
    mouse_press(widget, cell_pos(widget, 0, 9))
    mouse_move(widget, cell_pos(widget, 0, 2))
    mouse_release(widget, cell_pos(widget, 0, 2))
    # a click at the drag end: fresh click #1 → cancels (a word
    # selection would copy "llo worl")
    mouse_press(widget, cell_pos(widget, 0, 2))
    mouse_release(widget, cell_pos(widget, 0, 2))
    assert widget._selection is None
    press(widget, Qt.Key.Key_C, copy_mods())
    assert clipboard().text() == ""


def test_redrag_from_drag_end_is_a_fresh_drag(
    widget: TerminalWidget, session: Session, fake: FakePty, qtbot: QtBot
) -> None:
    """A drag that moves cells is not a click — the click counter must
    not arm, so an immediate second press-drag backwards from the drag's
    end cell is a fresh drag, not click #2 (a word selection)."""
    fake.output(b"hello worl")
    wait_rows(widget, qtbot, "hello worl")
    clipboard().setText("")
    # backwards drag (0,9) -> (0,2)
    mouse_press(widget, cell_pos(widget, 0, 9))
    mouse_move(widget, cell_pos(widget, 0, 2))
    mouse_release(widget, cell_pos(widget, 0, 2))
    # immediately press-drag backwards from the end cell (0,2) -> (0,0)
    mouse_press(widget, cell_pos(widget, 0, 2))
    mouse_move(widget, cell_pos(widget, 0, 0))
    mouse_release(widget, cell_pos(widget, 0, 0))
    press(widget, Qt.Key.Key_C, copy_mods())
    assert clipboard().text() == "hel"


def test_alt_drag_selects_a_rectangle(
    widget: TerminalWidget, session: Session, fake: FakePty, qtbot: QtBot
) -> None:
    fake.output(b"ab\r\ncd")
    wait_rows(widget, qtbot, "ab", 0)
    wait_rows(widget, qtbot, "cd", 1)
    clipboard().setText("")
    alt = Qt.KeyboardModifier.AltModifier
    mouse_press(widget, cell_pos(widget, 0, 0), mods=alt)
    mouse_move(widget, cell_pos(widget, 1, 0), mods=alt)
    mouse_release(widget, cell_pos(widget, 1, 0), mods=alt)
    press(widget, Qt.Key.Key_C, copy_mods())
    assert clipboard().text() == "a\nc"


def test_copy_without_selection_is_a_noop(
    widget: TerminalWidget, session: Session, fake: FakePty, qtbot: QtBot
) -> None:
    clipboard().setText("old")
    press(widget, Qt.Key.Key_C, copy_mods())
    assert clipboard().text() == "old"


def test_middle_click_pastes(
    widget: TerminalWidget, session: Session, fake: FakePty, qtbot: QtBot
) -> None:
    clipboard().setText("xyz")
    pos = cell_pos(widget, 0, 0)
    mouse_press(widget, pos, Qt.MouseButton.MiddleButton)
    mouse_release(widget, pos, Qt.MouseButton.MiddleButton)
    qtbot.waitUntil(lambda: b"xyz" in fake.sent)
    assert fake.sent.endswith(b"xyz")


def test_scrolling_clears_the_selection(
    widget: TerminalWidget, session: Session, fake: FakePty, qtbot: QtBot
) -> None:
    fake.output(b"a\nb\nc\nd\ne\nf\ng\nh\ni\nj")
    qtbot.waitUntil(lambda: widget._scrollback_len >= 5)
    clipboard().setText("")
    mouse_press(widget, cell_pos(widget, 0, 0))
    mouse_move(widget, cell_pos(widget, 0, 1))
    mouse_release(widget, cell_pos(widget, 0, 1))
    wheel(widget, 120)  # viewport scroll → selection invalidated
    qtbot.waitUntil(lambda: widget._offset == 3)
    press(widget, Qt.Key.Key_C, copy_mods())
    assert clipboard().text() == ""


def cell_color(widget: TerminalWidget, row: int, col: int) -> QColor:
    """The backing-image color at a cell's center."""
    r = widget._renderer
    return widget._image.pixelColor(
        round(col * r.cell_w + r.cell_w / 2), row * r.cell_h + r.cell_h // 2
    )


def test_output_clears_the_selection(
    widget: TerminalWidget, session: Session
) -> None:
    """New output changes the text under a selection — the selection is
    invalidated (the same rule as scrolling), so the selected cells
    never linger over the new content."""
    widget._apply_snapshot(
        Snapshot(
            dirty_rows=(),
            rows=_blank_rows(session),
            scrollback_len=0,
            viewport_offset=0,
            cursor=(-1, 0),
        )
    )
    pos = cell_pos(widget, 0, 0)
    mouse_press(widget, pos)
    mouse_move(widget, cell_pos(widget, 0, 2))
    mouse_release(widget, cell_pos(widget, 0, 2))
    assert widget._selection is not None
    assert cell_color(widget, 0, 0) == DEFAULT_FG  # selected: reversed block

    widget._apply_snapshot(
        Snapshot(
            dirty_rows=(0,),
            rows=_blank_rows(session),
            scrollback_len=0,
            viewport_offset=0,
            cursor=(-1, 0),
            content_changed=True,  # the program wrote over the viewport
        )
    )
    assert widget._selection is None
    assert cell_color(widget, 0, 0) == DEFAULT_BG  # the reversed cell is gone


def test_cursor_move_snapshot_keeps_the_selection(
    widget: TerminalWidget, session: Session
) -> None:
    """A cursor move repaints its old and new rows without changing text
    — the selection's copy payload is still correct, so it survives (and
    its reversed cell stays)."""
    widget._apply_snapshot(
        Snapshot(
            dirty_rows=(),
            rows=_blank_rows(session),
            scrollback_len=0,
            viewport_offset=0,
            cursor=(-1, 0),
        )
    )
    pos = cell_pos(widget, 0, 0)
    mouse_press(widget, pos)
    mouse_move(widget, cell_pos(widget, 0, 2))
    mouse_release(widget, cell_pos(widget, 0, 2))
    assert widget._selection is not None

    widget._apply_snapshot(
        Snapshot(
            dirty_rows=(0,),  # old and new cursor rows repaint…
            rows=_blank_rows(session),
            scrollback_len=0,
            viewport_offset=0,
            cursor=(0, 3),  # …but the cursor only moved: no text changed
        )
    )
    assert widget._selection is not None
    assert cell_color(widget, 0, 0) == DEFAULT_FG  # still reversed


# -- Mouse protocol (DECSET ?1000 X10 / ?1006 SGR) -------------------------


def test_protocol_click_sends_sgr_press_and_release(
    widget: TerminalWidget, session: Session, fake: FakePty, qtbot: QtBot
) -> None:
    enable_mouse(widget, qtbot, "1000;1006")
    pos = cell_pos(widget, 3, 4)  # 1-based: row 4, col 5
    mouse_press(widget, pos)
    mouse_release(widget, pos)
    qtbot.waitUntil(lambda: fake.sent == b"\x1b[<0;5;4M\x1b[<0;5;4m")
    # The app owns the mouse: no selection is created (copy is a noop).
    clipboard().setText("")
    press(widget, Qt.Key.Key_C, copy_mods())
    assert clipboard().text() == ""


def test_protocol_drag_sends_motion_tracking(
    widget: TerminalWidget, session: Session, fake: FakePty, qtbot: QtBot
) -> None:
    enable_mouse(widget, qtbot, "1000;1002;1006")
    p0, p2 = cell_pos(widget, 0, 0), cell_pos(widget, 2, 0)
    mouse_press(widget, p0)
    mouse_move(widget, p2)
    mouse_release(widget, p2)
    qtbot.waitUntil(lambda: fake.sent == b"\x1b[<0;1;1M\x1b[<32;1;3M\x1b[<0;1;3m")


def test_protocol_release_without_press_sends_nothing(
    widget: TerminalWidget, session: Session, fake: FakePty, qtbot: QtBot
) -> None:
    enable_mouse(widget, qtbot, "1000;1006")
    mouse_release(widget, cell_pos(widget, 1, 1))
    assert fake.sent == b""  # release without press sends nothing


def test_protocol_shift_click_adds_the_modifier_bit(
    widget: TerminalWidget, session: Session, fake: FakePty, qtbot: QtBot
) -> None:
    enable_mouse(widget, qtbot, "1000;1006")
    pos = cell_pos(widget, 1, 1)
    mouse_press(widget, pos, mods=Qt.KeyboardModifier.ShiftModifier)
    mouse_release(widget, pos, mods=Qt.KeyboardModifier.ShiftModifier)
    qtbot.waitUntil(lambda: fake.sent == b"\x1b[<4;2;2M\x1b[<4;2;2m")


def test_protocol_wheel_goes_to_the_app_not_the_viewport(
    widget: TerminalWidget, session: Session, fake: FakePty, qtbot: QtBot
) -> None:
    fake.output(b"a\nb\nc\nd\ne\nf\ng\nh\ni\nj")
    qtbot.waitUntil(lambda: widget._scrollback_len >= 5)
    enable_mouse(widget, qtbot, "1000;1006")
    c = widget.rect().center()
    # the widget clamps protocol coordinates to the grid — mirror it
    col = min(int(c.x() // widget._renderer.cell_w + 1), widget._columns)
    row = min(c.y() // widget._renderer.cell_h + 1, widget._lines)
    wheel(widget, 120)
    qtbot.waitUntil(lambda: fake.sent == f"\x1b[<64;{col};{row}M".encode())
    assert widget._offset == 0  # no viewport scroll


def test_protocol_x10_without_sgr_encoding(
    widget: TerminalWidget, session: Session, fake: FakePty, qtbot: QtBot
) -> None:
    # ?1000 without ?1006: the app expects the X10 encoding.
    enable_mouse(widget, qtbot, "1000")
    pos = cell_pos(widget, 1, 1)  # 1-based (2, 2)
    mouse_press(widget, pos)
    mouse_release(widget, pos)
    qtbot.waitUntil(
        lambda: fake.sent == b"\x1b[M" + bytes((32, 34, 34)) + b"\x1b[M" + bytes((35, 34, 34))
    )


def test_protocol_x10_wheel_uses_buttons_4_and_5(
    widget: TerminalWidget, session: Session, fake: FakePty, qtbot: QtBot
) -> None:
    # ?1000 without ?1006: the wheel still forwards — as the legacy
    # buttons 4/5 (xterm parity), so X10-only apps scroll on it.
    enable_mouse(widget, qtbot, "1000")
    c = widget.rect().center()
    col = min(int(c.x() // widget._renderer.cell_w + 1), widget._columns)
    row = min(c.y() // widget._renderer.cell_h + 1, widget._lines)
    wheel(widget, 120)
    qtbot.waitUntil(
        lambda: fake.sent == b"\x1b[M" + bytes((32 + 4, 32 + col, 32 + row))
    )
    wheel(widget, -120)
    qtbot.waitUntil(
        lambda: fake.sent
        == b"\x1b[M" + bytes((32 + 4, 32 + col, 32 + row)) + b"\x1b[M" + bytes((32 + 5, 32 + col, 32 + row))
    )


def test_alt_screen_wheel_moves_the_cursor_line_by_line(
    widget: TerminalWidget, session: Session, fake: FakePty, qtbot: QtBot
) -> None:
    # Full-screen apps without mouse tracking (nano, man) have no
    # scrollback on the alternate screen — the wheel becomes Up/Down
    # arrows (iTerm2/Terminal.app behavior): the cursor moves line by
    # line instead of the viewport scrolling (which would do nothing).
    fake.output(b"\x1b[?1049h")
    qtbot.waitUntil(lambda: widget._alt_screen, timeout=5000)
    wheel(widget, 120)  # up → Up arrow
    qtbot.waitUntil(lambda: fake.sent == b"\x1b[A")
    wheel(widget, -120)  # down → Down arrow
    qtbot.waitUntil(lambda: fake.sent == b"\x1b[A\x1b[B")
    assert widget._offset == 0  # no viewport scroll
    fake.output(b"\x1b[?1049l")  # back to the normal screen
    qtbot.waitUntil(lambda: not widget._alt_screen, timeout=5000)
    wheel(widget, 120)  # normal screen: viewport scroll again
    qtbot.wait(50)
    assert fake.sent == b"\x1b[A\x1b[B"


def test_alt_screen_wheel_uses_ss3_in_application_cursor_mode(
    widget: TerminalWidget, session: Session, fake: FakePty, qtbot: QtBot
) -> None:
    # Apps in application cursor mode (?1h) expect SS3 arrows.
    fake.output(b"\x1b[?1049h\x1b[?1h")
    qtbot.waitUntil(lambda: widget._alt_screen and widget._dec_ckm, timeout=5000)
    wheel(widget, 120)
    qtbot.waitUntil(lambda: fake.sent == b"\x1bOA")


def test_alt_screen_wheel_banks_subnotch_trackpad_deltas(
    widget: TerminalWidget, session: Session, fake: FakePty, qtbot: QtBot
) -> None:
    # A trackpad swipe delivers small deltas; four 30° ticks bank into
    # one 120° notch = one line, not four.
    fake.output(b"\x1b[?1049h")
    qtbot.waitUntil(lambda: widget._alt_screen, timeout=5000)
    for _ in range(4):
        wheel(widget, 30)
    qtbot.waitUntil(lambda: fake.sent == b"\x1b[A", timeout=5000)
    qtbot.wait(50)
    assert fake.sent == b"\x1b[A"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS ⌘ semantics")
def test_command_c_copies_and_never_sends_sigint(
    widget: TerminalWidget, session: Session, fake: FakePty, qtbot: QtBot
) -> None:
    # Qt reports ⌘ as ControlModifier on macOS — it must be copy, not
    # the VINTR byte (the physical ⌃ key is the real Ctrl there).
    clipboard().setText("")
    press(widget, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert fake.sent == b""
    assert clipboard().text() == ""


def test_back_to_front_drag_reanchors_inside_old_selection(
    widget: TerminalWidget, session: Session, fake: FakePty, qtbot: QtBot
) -> None:
    """Selecting back-to-front (right-to-left) from inside a previous
    selection must re-anchor at the press cell — extending from the
    stale anchor would select the wrong text."""
    fake.output(b"hello worl")  # 10 cols: the grid's full width
    wait_rows(widget, qtbot, "hello worl")
    clipboard().setText("")
    # First select the whole line rightward.
    mouse_press(widget, cell_pos(widget, 0, 0))
    mouse_move(widget, cell_pos(widget, 0, 10))
    mouse_release(widget, cell_pos(widget, 0, 10))
    # Then drag back-to-front, starting inside the old selection:
    # press at col 8, drag left to col 4 → cols 4..8 ("o wor").
    mouse_press(widget, cell_pos(widget, 0, 8))
    mouse_move(widget, cell_pos(widget, 0, 4))
    mouse_release(widget, cell_pos(widget, 0, 4))
    press(widget, Qt.Key.Key_C, copy_mods())
    assert clipboard().text() == "o wor"


def test_first_drag_back_to_front_selects(
    widget: TerminalWidget, session: Session, fake: FakePty, qtbot: QtBot
) -> None:
    """A first-action right-to-left drag (no prior selection) must select
    the dragged range, normalized so col1 is the left edge."""
    fake.output(b"hello worl")  # 10 cols: the grid's full width
    wait_rows(widget, qtbot, "hello worl")
    clipboard().setText("")
    mouse_press(widget, cell_pos(widget, 0, 9))
    mouse_move(widget, cell_pos(widget, 0, 2))
    mouse_release(widget, cell_pos(widget, 0, 2))
    assert widget._selection == Selection(row1=0, col1=2, row2=0, col2=9, rectangular=False)
    press(widget, Qt.Key.Key_C, copy_mods())
    assert clipboard().text() == "llo worl"


def test_first_drag_bottom_to_top_selects(
    widget: TerminalWidget, session: Session, fake: FakePty, qtbot: QtBot
) -> None:
    """A first-action down-to-up drag must select rows from the press
    row back to the drag row, normalized so row1 is the top edge. The
    first row spans col1..end, the last row 0..col2 (open-ended contract
    of `column_range`, matching xterm/kitty)."""
    fake.output(b"hello\r\nworld")
    wait_rows(widget, qtbot, "hello", 0)
    wait_rows(widget, qtbot, "world", 1)
    clipboard().setText("")
    mouse_press(widget, cell_pos(widget, 1, 3))
    mouse_move(widget, cell_pos(widget, 0, 1))
    mouse_release(widget, cell_pos(widget, 0, 1))
    assert widget._selection == Selection(row1=0, col1=1, row2=1, col2=3, rectangular=False)
    press(widget, Qt.Key.Key_C, copy_mods())
    assert clipboard().text() == "ello\nworl"


# -- viewport-row merge (merge_viewport) ---------------------------------


def test_merge_full_replaces_rows() -> None:
    fake = FakePty()
    s = make_session(fake, lines=3, columns=4)
    try:
        fake.output(b"ab\r\ncd\r\nef")
        wait_content(s, "a")
        force_full(s)
        snap = wait_full(s, 3)
        rows = merge_viewport(snap, None)
        assert rows is not None
        assert [r.cells[0].data for r in rows] == ["a", "c", "e"]
        # A second full snapshot replaces wholesale.
        fake.output(b"\x1b[1;1Hxy")
        wait_content(s, "x")
        force_full(s)
        snap2 = wait_full(s, 3)
        rows2 = merge_viewport(snap2, rows)
        assert [r.cells[0].data for r in rows2] == ["x", "c", "e"]
    finally:
        s.close()


def test_merge_partial_overwrites_only_dirty_rows() -> None:
    fake = FakePty()
    s = make_session(fake, lines=3, columns=4)
    try:
        fake.output(b"ab\r\ncd\r\nef")
        wait_content(s, "a")
        force_full(s)
        snap = wait_full(s, 3)
        rows = merge_viewport(snap, None)
        assert rows is not None
        before = [r.cells[0].data for r in rows]
        # Rewrite only row 1 — the incremental snapshot carries one row.
        fake.output(b"\x1b[2;1Hzy")
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            snap2 = s.snapshots[-1]
            if not snap2.full and snap2.dirty_rows:
                break
            time.sleep(0.01)
        else:
            raise TimeoutError("no incremental snapshot")
        rows = merge_viewport(snap2, rows)
        assert [r.cells[0].data for r in rows] == [before[0], "z", before[2]]
    finally:
        s.close()


def test_merge_partial_before_first_full_stays_none() -> None:
    # Synthetic: an incremental snapshot with no prior rows leaves None.
    snap = Snapshot(
        dirty_rows=(1,),
        rows=(),
        scrollback_len=0,
        viewport_offset=0,
        cursor=(0, 0),
        dec_ckm=False,
        bracketed_paste=False,
        reverse_video=False,
        full=False,
    )
    assert merge_viewport(snap, None) is None


# -- partial rendering: repaint only the damaged region ------------------


def test_snapshot_rect_limited_to_dirty_rows(widget: TerminalWidget) -> None:
    ch = widget._renderer.cell_h
    partial = Snapshot(
        dirty_rows=(1,),
        rows=(),
        scrollback_len=0,
        viewport_offset=0,
        cursor=(-1, 0),
        full=False,
    )
    rect = widget._snapshot_rect(partial)
    assert rect.top() == 1 * ch  # one row
    assert rect.height() == 1 * ch
    assert rect.width() == widget.sizeHint().width()

    full = Snapshot(
        dirty_rows=(),
        rows=(),
        scrollback_len=0,
        viewport_offset=0,
        cursor=(-1, 0),
        full=True,
    )
    assert widget._snapshot_rect(full) == QRect(0, 0, widget.sizeHint().width(), widget.sizeHint().height())

    mode_only = Snapshot(
        dirty_rows=(),
        rows=(),
        scrollback_len=0,
        viewport_offset=0,
        cursor=(-1, 0),
        full=False,
    )
    assert widget._snapshot_rect(mode_only).isEmpty()  # nothing visible changed


# -- cursor blink ---------------------------------------------------------


def focus(widget: TerminalWidget) -> None:
    """Give the widget focus via a synthetic FocusIn (the offscreen
    platform never delivers real focus events) — starts the blink."""
    QApplication.sendEvent(widget, QFocusEvent(QEvent.Type.FocusIn))


def unfocus(widget: TerminalWidget) -> None:
    QApplication.sendEvent(widget, QFocusEvent(QEvent.Type.FocusOut))


def test_cursor_blink_focus_gates_the_timer(widget: TerminalWidget) -> None:
    """xterm behavior: the cursor blinks only while the widget has
    focus — the timer runs on FocusIn and stops on FocusOut, freezing
    the cursor solid."""
    assert not widget._cursor_blink_timer.isActive()  # unfocused: no blink
    focus(widget)
    assert widget._cursor_blink_timer.isActive()
    assert widget._cursor_blink is True  # solid on focus
    unfocus(widget)
    assert not widget._cursor_blink_timer.isActive()
    assert widget._cursor_blink is True  # frozen solid


def test_cursor_blink_timer_toggles_phase(widget: TerminalWidget, qtbot: QtBot) -> None:
    """Focused: the timer flips the phase at its interval and the block
    appears/disappears in the backing image (the minimal repaint)."""
    qtbot.waitUntil(lambda: widget._last_snapshot is not None)  # initial snapshot applied
    widget._cursor_blink_timer.setInterval(50)  # speed up the test
    focus(widget)
    assert widget._cursor_blink is True
    assert cell_color(widget, 0, 0) == DEFAULT_FG  # block visible
    qtbot.waitUntil(lambda: widget._cursor_blink is False, timeout=2000)
    assert cell_color(widget, 0, 0) == DEFAULT_BG  # block hidden
    qtbot.waitUntil(lambda: widget._cursor_blink is True, timeout=2000)
    assert cell_color(widget, 0, 0) == DEFAULT_FG  # block back


def test_cursor_blink_dectcem_overwrites_phase(
    widget: TerminalWidget, fake: FakePty, qtbot: QtBot
) -> None:
    """DECTCEM ?25 overwrites the blink phase on every snapshot: ?25l
    forces the cursor hidden (and a blink tick must not resurrect it),
    ?25h re-anchors it visible — the timer free-runs underneath."""
    qtbot.waitUntil(lambda: widget._last_snapshot is not None)
    focus(widget)
    fake.output(b"\x1b[?25l")  # hide the cursor
    qtbot.waitUntil(lambda: widget._cursor_blink is False, timeout=2000)
    assert cell_color(widget, 0, 0) == DEFAULT_BG  # block gone
    assert widget._cursor_blink_timer.isActive()  # the timer keeps running
    widget._toggle_cursor_blink()  # a tick flips the phase…
    assert cell_color(widget, 0, 0) == DEFAULT_BG  # …but the gate keeps it hidden
    fake.output(b"\x1b[?25h")  # show it
    qtbot.waitUntil(
        lambda: widget._last_snapshot is not None and widget._last_snapshot.cursor_visible,
        timeout=2000,
    )
    assert widget._cursor_blink is True  # re-anchored visible
    assert cell_color(widget, 0, 0) == DEFAULT_FG  # solid again


def test_cursor_blink_activity_reset_on_keypress(
    widget: TerminalWidget, qtbot: QtBot
) -> None:
    """Typing snaps the cursor solid immediately — no disorienting
    mid-hidden-phase cursor under the fingers."""
    qtbot.waitUntil(lambda: widget._last_snapshot is not None)
    widget._cursor_blink_timer.setInterval(50)
    focus(widget)
    qtbot.waitUntil(lambda: widget._cursor_blink is False, timeout=2000)  # mid-hidden-phase
    press(widget, Qt.Key.Key_A, text="a")
    assert widget._cursor_blink is True  # the keypress snapped it solid
    assert cell_color(widget, 0, 0) == DEFAULT_FG


def test_cursor_blink_repaints_only_cursor_row(
    widget: TerminalWidget, session: Session
) -> None:
    """A blink tick re-renders only the cursor row — row 0's pixels
    survive untouched and the update rect covers exactly one row."""
    rects: list[QRect] = []
    widget.update = lambda rect: rects.append(rect)  # type: ignore[method-assign]
    ch = widget._renderer.cell_h
    rows = (
        Row([Cell("M")] + [Cell.blank() for _ in range(session.columns - 1)]),
        Row([Cell.blank() for _ in range(session.columns)]),
    )
    widget._apply_snapshot(
        Snapshot(
            dirty_rows=(0, 1),
            rows=rows,
            scrollback_len=0,
            viewport_offset=0,
            cursor=(1, 0),
        )
    )
    assert cell_color(widget, 1, 0) == DEFAULT_FG  # cursor block on row 1
    before = [widget._image.pixelColor(x, 0) for x in range(widget._image.width())]
    widget._cursor_blink = False
    widget._repaint_cursor()
    assert cell_color(widget, 1, 0) == DEFAULT_BG  # block hidden
    after = [widget._image.pixelColor(x, 0) for x in range(widget._image.width())]
    assert before == after  # row 0 untouched
    assert rects[-1] == QRect(0, 1 * ch, widget._image.width(), ch)  # one row


def test_cursor_blink_inverts_character_under_it(
    widget: TerminalWidget, session: Session
) -> None:
    """A blink tick on a text cell shows the character inverted (block =
    the cell's fg, glyph = its bg), and the hidden phase restores the
    plain text — the cursor never hides the character it sits on. The
    dominant color flips: red (the fg block) while the cursor is on the
    cell, blue (the bg) once it blinks away."""
    red = rgb(255, 0, 0)
    blue = rgb(0, 0, 255)
    rows = (
        Row([Cell("X", fg=red, bg=blue)] + [Cell.blank() for _ in range(session.columns - 1)]),
        Row([Cell.blank() for _ in range(session.columns)]),
    )
    widget._apply_snapshot(
        Snapshot(
            dirty_rows=(0, 1),
            rows=rows,
            scrollback_len=0,
            viewport_offset=0,
            cursor=(0, 0),
        )
    )

    def count(color: QColor) -> int:
        r = widget._renderer
        n = 0
        for y in range(r.cell_h):
            for x in range(round(r.cell_w)):
                if widget._image.pixelColor(x, y) == color:
                    n += 1
        return n

    assert count(QColor(255, 0, 0)) > count(QColor(0, 0, 255))  # inverted: fg block
    widget._cursor_blink = False
    widget._repaint_cursor()
    assert count(QColor(0, 0, 255)) > count(QColor(255, 0, 0))  # plain: bg block


def test_cursor_outline_when_unfocused(
    widget: TerminalWidget, session: Session
) -> None:
    """Unfocused: the cursor becomes a hollow rectangle around the cell
    — the character underneath stays visible (no block, no inversion);
    refocusing restores the block. The dominant color flips: red (the
    fg block) while focused, blue (the bg) once unfocused."""
    red = rgb(255, 0, 0)
    blue = rgb(0, 0, 255)
    rows = (
        Row([Cell("X", fg=red, bg=blue)] + [Cell.blank() for _ in range(session.columns - 1)]),
        Row([Cell.blank() for _ in range(session.columns)]),
    )
    widget._apply_snapshot(
        Snapshot(
            dirty_rows=(0, 1),
            rows=rows,
            scrollback_len=0,
            viewport_offset=0,
            cursor=(0, 0),
        )
    )

    def count(color: QColor) -> int:
        r = widget._renderer
        n = 0
        for y in range(r.cell_h):
            for x in range(round(r.cell_w)):
                if widget._image.pixelColor(x, y) == color:
                    n += 1
        return n

    focus(widget)
    assert widget._cursor_style == "block"
    assert count(QColor(255, 0, 0)) > count(QColor(0, 0, 255))  # inverted: fg block

    unfocus(widget)
    assert widget._cursor_style == "outline"
    assert count(QColor(0, 0, 255)) > count(QColor(255, 0, 0))  # plain: bg block
    r = widget._renderer
    assert any(
        widget._image.pixelColor(x, y) == DEFAULT_FG
        for y in range(r.cell_h)
        for x in range(round(r.cell_w))
    )  # the outline is drawn

    focus(widget)
    assert widget._cursor_style == "block"
    assert count(QColor(255, 0, 0)) > count(QColor(0, 0, 255))  # inverted again
