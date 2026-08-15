# -*- coding: utf-8 -*-
# Copyright (C) 2018-2026 Connet Information Technology Company, Shanghai.
"""The terminal widgets (Slice B) — render snapshots and encode keys,
and nothing else.

The GUI never reads the model (ADR-0005): snapshots arrive from the
reader thread through a queued signal; the widgets mirror the input-path
mode flags (`dec_ckm`, `bracketed_paste`) and the scrollbar state from
the snapshot payloads, post commands (send/resize/scroll) back, and
derive their grid geometry from their own size and the font metrics.

The paint backend:

- :class:`TerminalWidget` — the CPU path: snapshots render into a
  persistent QImage (partial snapshots repaint only their dirty rows),
  `paintEvent` blits the damaged region. Also the test seam — pixel
  checks read the image offscreen. This is the shipping backend
  (`pyqtermx/__main__.py`).

Paint events are scheduled with `update(QRect)` limited to the region
the snapshot changed (partial rendering: a one-row update repaints one
row, not the whole frame). A full repaint — the whole grid in the
damaged region — re-renders the backing from the merged viewport
instead of blitting it: the frame heals itself from the last snapshot,
stale or blank pixels included (the compositor may have dropped the
window surface while the display slept).

Key handling (spec Q8): printables and control keys encode via
`encode_key` and go to the child, followed by `scroll_to_bottom()`;
PgUp/PgDn scroll the viewport when history exists (they reach the child
only on the alternate screen, where there is none); Ctrl+Shift+V and
Shift+Insert paste (bracketed when the app asked for `?2004`); ⌘+C
(macOS) / Ctrl+Shift+C copy the selection.

Mouse (the modern-terminal set): a single click cancels the selection,
click-drag selects, double-click a word, triple-click a line, Alt-drag
a rectangle; the selection renders reversed and copies to the
clipboard (local actions — the terminal never sends bytes for them).
When the app enables mouse tracking
(`?1000` X10, `?1002` button-event, `?1003` any-event, `?1006` SGR) the
mouse belongs to it: clicks and wheel forward as protocol events, the
wheel stops scrolling the viewport, and selection is disabled.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from PyQt6.QtCore import QEvent, QPoint, QRect, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QCloseEvent,
    QFocusEvent,
    QImage,
    QInputMethodEvent,
    QKeyEvent,
    QMouseEvent,
    QPaintEvent,
    QPainter,
    QResizeEvent,
    QWheelEvent,
    QFont,
    QColor
)
from PyQt6.QtWidgets import QApplication, QScrollBar, QWidget

from pyqtermx.input import (
    CTRL_MOD,
    encode_arrow_key,
    encode_key,
    encode_mouse_x10,
    encode_paste,
    encode_sgr_mouse,
)
from pyqtermx.render import CURSOR_BLOCK, CURSOR_OUTLINE, TerminalRenderer
from pyqtermx.screen import Row
from pyqtermx.selection import Selection, extend, line, selected_text, word
from pyqtermx.session import Session, Snapshot

if TYPE_CHECKING:
    # The mixin is a plain object at runtime (PyQt6 doesn't chain
    # super().__init__ through a QObject-derived mixin — and object is
    # the only base that keeps the C3 MRO of both concrete classes
    # valid). For mypy alone it inherits QWidget, so the QWidget API
    # the mixin uses checks out.
    _QtBase = QWidget
else:
    _QtBase = object

#: Wheel: scroll this many viewport rows per 120° notch.
WHEEL_ROWS = 3

#: Resize debounce: wait this long of stable size before resizing the pty.
RESIZE_DEBOUNCE_MS = 50

#: Cursor blink: toggle the cursor's visibility at this cadence while
#: the widget has focus (xterm behavior — unfocused cursors stay solid).
CURSOR_BLINK_MS = 500

#: The default terminal geometry until a resize arrives.
DEFAULT_LINES = 24
DEFAULT_COLUMNS = 80


def merge_viewport(snapshot: Snapshot, prev: list[Row] | None) -> list[Row] | None:
    """Fold a snapshot into the widget's persistent viewport grid.
    `full` snapshots replace everything; incremental snapshots
    overwrite only their dirty rows. `None` until the first `full`
    snapshot arrives — the session always leads with one."""
    if snapshot.full:
        return list(snapshot.rows)
    if prev is None:
        return None
    for k, row in zip(snapshot.dirty_rows, snapshot.rows):
        prev[k] = row
    return prev


class TerminalMixin(_QtBase):
    """Everything both paint backends share: the session bridge, input
    encoding, scrollbar, resize debounce and mode mirroring. A plain
    object mixin (PyQt6 doesn't chain super().__init__ through a
    QObject-derived one) — each concrete class defines `_snapshot_ready`
    and implements the `_apply_snapshot`/`_rebuild_backing`/
    `_resize_backing` hooks, `paintEvent` and `sizeHint`."""

    #: Snapshot delivery — declared on each concrete class (pyqtSignal
    #: needs a real class body); declared here for the type checker.
    if TYPE_CHECKING:
        _snapshot_ready = pyqtSignal(object)  # Snapshot

    def __init__(self, session: Session | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        # IME (Chinese/Japanese/etc.) delivers composed text as
        # QInputMethodEvent — only widgets with input methods enabled
        # receive them (spec Q8).
        self.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled)
        self._renderer = TerminalRenderer(antialias=True)
        self._lines = DEFAULT_LINES
        self._columns = DEFAULT_COLUMNS
        self._last_snapshot: Snapshot | None = None

        # The merged viewport rows (both backends): full snapshots
        # replace it, incremental snapshots overwrite the dirty rows —
        # selection and copy need every row, not just the dirty ones.
        self._viewport_rows: list[Row] | None = None

        # Mouse: the selection (viewport coordinates) and the
        # press/drag state. The selection is a *local* action — the
        # terminal renders it and copies it, never sends bytes.
        self._selection: Selection | None = None
        self._mouse_dragging = False
        self._mouse_drag_button = 0
        # Press/drag state: the selection is extended from the press
        # cell (never from the selection's own start — that drifts once
        # a backwards drag pushes the anchor to the far end) until the
        # release. A single click cancels the selection; only a drag
        # (or double/triple-click) creates one.
        self._press_anchor: tuple[int, int] | None = None
        self._press_rectangular = False
        self._click_count = 1
        self._last_click_pos: Any = None
        self._last_click_time = 0.0

        self._scrollbar = QScrollBar(Qt.Orientation.Vertical, self)
        self._scrollbar.valueChanged.connect(self._on_scrollbar)
        self._scrollbar.hide()

        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(RESIZE_DEBOUNCE_MS)
        self._resize_timer.timeout.connect(self._apply_resize)

        # Cursor blink: the widget's own phase, toggled at
        # `CURSOR_BLINK_MS` while focused; the renderer ANDs it with
        # the snapshot's DECTCEM visibility, so `?25l`/`?25h` always
        # win and the app can hide the cursor mid-blink. Every snapshot
        # re-anchors the phase to the DECTCEM value (new output snaps
        # the cursor solid); keypresses do too.
        self._cursor_blink = True
        self._cursor_blink_timer = QTimer(self)
        self._cursor_blink_timer.setInterval(CURSOR_BLINK_MS)
        self._cursor_blink_timer.timeout.connect(self._toggle_cursor_blink)
        #: The cursor's look: the focused block (inverted character) or
        #: the unfocused hollow rectangle around the cell.
        self._cursor_style = CURSOR_BLOCK

        # Input-path mode flags, mirrored from snapshots (spec Q8).
        self._dec_ckm = False
        self._bracketed_paste = False
        self._mouse_1000 = False
        self._mouse_1002 = False
        self._mouse_1003 = False
        self._mouse_1006 = False
        self._alt_screen = False
        #: Sub-notch wheel deltas (trackpad) banked for the alt-screen
        #: page-key path — one full 120° notch pages the app once.
        self._wheel_accum = 0
        self._scrollback_len = 0
        self._offset = 0

        self._session: Session | None = None
        # AutoConnection: the signal is emitted on the reader thread, so
        # delivery to this GUI-thread widget is queued automatically.
        self._snapshot_ready.connect(self._apply_snapshot)
        if session is not None:
            self.set_session(session)

    # -- Backend hooks (implemented by each paint backend) ----------------

    def _apply_snapshot(self, snapshot: Snapshot) -> None:
        """Backend hook: repaint `snapshot`, mirror flags, update."""
        raise NotImplementedError

    def _rebuild_backing(self) -> None:
        """Backend hook: rebuild the backing store for a new geometry."""
        raise NotImplementedError

    def _resize_backing(self) -> None:
        """Backend hook: adapt the backing store to a resize."""
        raise NotImplementedError

    def _refresh(self) -> None:
        """Backend hook: repaint with the current selection (re-render
        the backing image — it is the source of truth)."""
        raise NotImplementedError

    def _repaint_cursor(self) -> None:
        """Backend hook: re-render the cursor row with the current
        blink phase — the minimal repaint (one row, not the frame)."""
        raise NotImplementedError

    # -- Partial rendering -------------------------------------------------

    def _snapshot_rect(self, snapshot: Snapshot) -> QRect:
        """The pixel rect a snapshot changed — `update()` gets exactly
        this, so a paint event covers only the damaged region, not the
        whole frame (partial rendering). `full` snapshots repaint the
        whole grid; snapshots with no dirty rows changed nothing
        visible (invisible mode changes) and repaint nothing."""
        width = round(self._columns * self._renderer.cell_w)
        height = self._lines * self._renderer.cell_h
        if snapshot.full:
            return QRect(0, 0, width, height)
        if not snapshot.dirty_rows:
            return QRect()
        first = min(snapshot.dirty_rows)
        last = max(snapshot.dirty_rows)
        return QRect(
            0,
            first * self._renderer.cell_h,
            width,
            (last - first + 1) * self._renderer.cell_h,
        )

    def _request_repaint(self, snapshot: Snapshot) -> None:
        """Schedule a repaint of exactly the region `snapshot` changed."""
        rect = self._snapshot_rect(snapshot)
        if not rect.isEmpty():
            self.update(rect)

    def _toggle_cursor_blink(self) -> None:
        """Blink tick: flip the cursor phase and repaint only the
        cursor row (partial rendering — the renderer ANDs the phase
        with the snapshot's DECTCEM visibility, so a cursor the app
        hid stays hidden)."""
        self._cursor_blink = not self._cursor_blink
        self._repaint_cursor()

    # -- Session bridge ---------------------------------------------------

    def set_session(self, session: Session) -> None:
        """Attach the session: adopt its geometry, repaint its latest
        state, and deliver every following snapshot on the GUI thread."""
        self._session = session
        session.snapshot_callback = self._on_session_snapshot
        self._lines, self._columns = session.lines, session.columns
        self._rebuild_backing()
        if session.snapshots:
            self._apply_snapshot(session.snapshots[-1])
        if self.hasFocus():
            self._cursor_blink_timer.start()

    def set_font(self, font: QFont) -> None:
        """Replace the glyph font, re-derive the grid geometry from the
        new cell metrics, rebuild the backing, and re-render the last
        snapshot. The pty is resized to the new grid size (debounced
        like a widget resize)."""
        self._renderer.set_font(font)
        if self._session is not None:
            lines = max(1, self.height() // self._renderer.cell_h)
            columns = max(1, int(self.width() // self._renderer.cell_w))
            self._lines, self._columns = lines, columns
            self._session.resize(lines, columns)
        self._rebuild_backing()
        self._refresh()

    def set_palette(self, fg: QColor, bg: QColor) -> None:
        """Replace the terminal's default colors and repaint the last
        snapshot with them. The emulator is told too, so OSC 10/11
        color queries (TUI theme detection) report the themed colors —
        a light-theme app embedding a terminal must not answer "dark"
        to the child's background query."""
        self._renderer.set_palette(fg, bg)
        if self._session is not None:
            self._session.set_palette(
                fg.name(QColor.NameFormat.HexRgb), bg.name(QColor.NameFormat.HexRgb)
            )
        self._refresh()

    def _on_session_snapshot(self, snapshot: Snapshot) -> None:
        """Runs on the reader thread — hand the snapshot to the GUI
        thread (queued connection, ADR-0005)."""
        self._snapshot_ready.emit(snapshot)

    def _mirror_flags(self, snapshot: Snapshot) -> None:
        """The scrollbar and input-path mode flags, mirrored from the
        snapshot payload."""
        self._dec_ckm = snapshot.dec_ckm
        self._bracketed_paste = snapshot.bracketed_paste
        self._mouse_1000 = snapshot.mouse_1000
        self._mouse_1002 = snapshot.mouse_1002
        self._mouse_1003 = snapshot.mouse_1003
        self._mouse_1006 = snapshot.mouse_1006
        self._alt_screen = snapshot.alt_screen
        self._scrollback_len = snapshot.scrollback_len
        self._offset = snapshot.viewport_offset
        # DECTCEM overwrites the blink phase on every snapshot: `?25h`
        # re-anchors it visible (new output snaps the cursor solid),
        # `?25l` forces it hidden — the timer free-runs underneath.
        self._cursor_blink = snapshot.cursor_visible
        self._update_scrollbar()

    def _update_scrollbar(self) -> None:
        scrollbar = self._scrollbar
        scrollbar.blockSignals(True)
        scrollbar.setRange(0, self._scrollback_len)
        scrollbar.setValue(self._scrollback_len - self._offset)  # top = oldest
        scrollbar.blockSignals(False)
        scrollbar.setVisible(self._scrollback_len > 0)
        self._position_scrollbar()

    def _position_scrollbar(self) -> None:
        """Right edge, full height."""
        extent = self._scrollbar.sizeHint().width()
        self._scrollbar.setGeometry(self.width() - extent, 0, extent, self.height())

    def _on_scrollbar(self, value: int) -> None:
        self._clear_selection()  # the viewport content changes under it
        if self._session is not None:
            target = self._scrollback_len - value
            self._session.scroll(target - self._offset)

    # -- Input (spec Q8) --------------------------------------------------

    def keyPressEvent(self, event: QKeyEvent | None) -> None:
        if event is None:
            return
        session = self._session
        if session is None:
            return
        # Activity reset: typing snaps the cursor solid immediately
        # (the echoed output re-anchors it again via the snapshot).
        self._cursor_blink = True
        self._repaint_cursor()
        data = encode_key(
            event,
            dec_ckm=self._dec_ckm,
            scrollback_len=self._scrollback_len,
        )
        if data is None:
            self._handle_local_key(event)
            return
        session.send_data(data)
        session.scroll_to_bottom()

    def inputMethodEvent(self, event: QInputMethodEvent | None) -> None:
        """IME input (spec Q8): forward the committed text to the child
        as UTF-8. The composition (preedit) has no on-screen
        representation in this terminal, so only the commit string is
        sent — the event is still accepted so the IME keeps working."""
        if event is None:
            return
        session = self._session
        if session is None:
            return
        text = event.commitString()
        if text:
            session.send_data(text.encode("utf-8"))
            session.scroll_to_bottom()
        event.accept()

    def inputMethodQuery(self, query: Qt.InputMethodQuery) -> Any:
        """IME geometry queries (spec Q8): the candidate window anchors
        to the cursor cell, in widget coordinates. Without
        ImCursorRectangle the IME falls back to a default position
        (below the window)."""
        if query == Qt.InputMethodQuery.ImCursorRectangle:
            snapshot = self._last_snapshot
            if snapshot is not None:
                row, col = snapshot.cursor
                row += self._offset  # grid row → viewport row
                if 0 <= row < self._lines:
                    return QRect(
                        round(col * self._renderer.cell_w),
                        round(row * self._renderer.cell_h),
                        round(self._renderer.cell_w),
                        round(self._renderer.cell_h),
                    )
            return QRect(0, 0, 0, 0)
        if query == Qt.InputMethodQuery.ImEnabled:
            return True
        if query == Qt.InputMethodQuery.ImFont:
            return self._renderer.font
        return super().inputMethodQuery(query)

    def _handle_local_key(self, event: QKeyEvent) -> None:
        """Local keys — handled by the terminal itself, never sent to
        the child (xterm's 'local' action category)."""
        session = self._session
        if session is None:
            return
        qkey = event.key()
        if qkey in (Qt.Key.Key_PageUp, Qt.Key.Key_PageDown):
            self._clear_selection()  # the viewport content changes
            sign = 1 if qkey == Qt.Key.Key_PageUp else -1  # scroll(n): + is up
            session.scroll(sign * self._lines)
        elif qkey == Qt.Key.Key_V and event.modifiers() & (
            Qt.KeyboardModifier.ControlModifier | CTRL_MOD
        ):
            self._paste()
        elif qkey == Qt.Key.Key_Insert and event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self._paste()
        elif qkey == Qt.Key.Key_C and event.modifiers() & (
            Qt.KeyboardModifier.ControlModifier | CTRL_MOD
        ):
            # ⌘+C on macOS (Qt reports ⌘ as Control) / Ctrl+Shift+C
            # elsewhere: copy the selection. Plain Ctrl+C is a control
            # code (SIGINT) and never reaches this branch.
            self._copy()

    def _copy(self) -> None:
        """Copy the selection — a local action: the selection is the
        terminal's, not the child's, so no bytes are sent (xterm's
        'local' copy)."""
        if self._selection is None:
            return
        rows = self._viewport_rows
        if not rows:
            return
        clipboard = QApplication.clipboard()
        if clipboard is None:
            return
        clipboard.setText(selected_text(rows, self._selection))

    def _clear_selection(self) -> None:
        """The viewport content changed under the selection: it selects
        *visible* rows, and scrolling or new output changes what the rows
        show — the GUI holds no scrollback text to re-identify
        (ADR-0005), so keeping the selection would copy the wrong text."""
        if self._selection is not None:
            self._selection = None
            self._refresh()

    def _paste(self) -> None:
        if self._session is None:
            return
        clipboard = QApplication.clipboard()
        if clipboard is None:
            return
        text = clipboard.text()
        self._session.send_data(encode_paste(text, bracketed_paste=self._bracketed_paste))
        self._session.scroll_to_bottom()

    def wheelEvent(self, event: QWheelEvent | None) -> None:
        if event is None or self._session is None:
            return
        delta = event.angleDelta().y()
        if delta == 0:
            return
        if self._mouse_enabled():
            # The app asked for the mouse: the wheel is its input (htop
            # scrolls its own list) — never the viewport's. SGR (?1006)
            # encodes wheel as buttons 64/65, legacy X10 as 4/5 (xterm
            # parity); wheel releases are not reported (xterm doesn't).
            self._wheel_accum = 0
            self._send_mouse(event, "wheel_up" if delta > 0 else "wheel_down", 0)
            return
        self._clear_selection()
        if self._alt_screen:
            # A full-screen app without mouse tracking (nano, man, less
            # with a mouse-less config…) has no scrollback to scroll —
            # the wheel becomes Up/Down arrows, so the cursor moves
            # line by line (iTerm2/Terminal.app behavior); apps that
            # set application cursor mode (?1) get SS3. Sub-notch
            # deltas bank up, so a trackpad swipe scrolls at a human
            # rate (one line per 120° notch) instead of firing an
            # arrow per event.
            self._wheel_accum += delta
            pages, self._wheel_accum = divmod(self._wheel_accum, 120)
            if pages:
                data = encode_arrow_key(
                    Qt.Key.Key_Up if pages > 0 else Qt.Key.Key_Down,
                    dec_ckm=self._dec_ckm,
                )
                for _ in range(abs(pages)):
                    self._session.send_data(data)
            return
        self._wheel_accum = 0
        steps = max(1, abs(delta) // 120) if abs(delta) >= 120 else 1
        self._session.scroll(steps * WHEEL_ROWS if delta > 0 else -steps * WHEEL_ROWS)

    # -- Mouse ----------------------------------------------------------

    _MOUSE_BUTTONS = {
        Qt.MouseButton.LeftButton: 0,
        Qt.MouseButton.MiddleButton: 1,
        Qt.MouseButton.RightButton: 2,
    }

    def _mouse_enabled(self) -> bool:
        """Mouse tracking active: the child owns the mouse (any of
        `?1000`/`?1002`/`?1003`), so clicks and wheel forward to it and
        selection is disabled."""
        return self._mouse_1000 or self._mouse_1002 or self._mouse_1003

    def _cell_at(self, pos: Any) -> tuple[int, int]:
        """The viewport (row, col) under a widget position, clamped."""
        row = int(pos.y()) // self._renderer.cell_h
        col = int(int(pos.x()) // self._renderer.cell_w)
        return (
            min(max(row, 0), self._lines - 1),
            min(max(col, 0), self._columns - 1),
        )

    def _send_mouse(self, event: Any, action: str, button: int) -> None:
        """Forward a mouse event to the child — SGR (`?1006`) or X10
        (`?1000`), the modern-terminal set. Coordinates are 1-based;
        modifiers map to the xterm bits (4 shift, 8 alt, 16 ctrl — ⌘
        counts as ctrl on macOS, matching kitty)."""
        session = self._session
        if session is None:
            return
        pos = event.position()
        col = min(max(int(int(pos.x()) // self._renderer.cell_w + 1), 1), self._columns)
        row = min(max(int(pos.y()) // self._renderer.cell_h + 1, 1), self._lines)
        mods = 0
        m = event.modifiers()
        if m & Qt.KeyboardModifier.ShiftModifier:
            mods += 4
        if m & Qt.KeyboardModifier.AltModifier:
            mods += 8
        if m & (Qt.KeyboardModifier.ControlModifier | CTRL_MOD):
            mods += 16
        if self._mouse_1006:
            data = encode_sgr_mouse(col, row, button, action=action, mods=mods)
        else:
            data = encode_mouse_x10(col, row, button, action=action, mods=mods)
        session.send_data(data)

    def _record_press(self, event: QMouseEvent) -> None:
        """Anchor the click-count clock at the press — Qt counts
        double-clicks from consecutive *press* positions, and a release
        position (e.g. a drag's end) must not look like a click."""
        self._last_click_pos = event.position().toPoint()
        self._last_click_time = time.monotonic()

    def _next_click_count(self, event: QMouseEvent) -> int:
        """Qt's click counting: a press at the same position within the
        double-click interval of the previous press is click #2, #3…
        (double-click events arrive as `mouseDoubleClickEvent` — the
        widget treats them as press #2 explicitly)."""
        now = time.monotonic()
        pos = event.position().toPoint()
        if (
            self._last_click_pos == pos
            and now - self._last_click_time < QApplication.doubleClickInterval() / 1000
        ):
            return self._click_count + 1
        return 1

    def _selection_press(self, event: QMouseEvent, count: int) -> None:
        """Start (or extend) the selection at the click cell: a single
        click cancels any selection (selection is drag-driven), a
        double-click selects the word, a triple-click the line; Alt
        switches to rectangular mode. The drag anchor is recorded here
        and never changes for the whole drag — extend() gets it
        explicitly."""
        self._mouse_dragging = True
        row, col = self._cell_at(event.position())
        rectangular = bool(event.modifiers() & Qt.KeyboardModifier.AltModifier)
        self._press_rectangular = rectangular
        if count == 1:
            # A bare click selects nothing — it cancels the selection.
            # The drag anchor is the press cell; the first cell-changing
            # move then creates the selection (drag-only selection).
            if self._selection is not None:
                self._selection = None
                self._refresh()
            self._press_anchor = (row, col)
            return
        if count >= 3:
            self._selection = line(row, self._columns)
        else:
            self._selection = word(row, col, self._viewport_rows or [])
        # The drag anchor: for word/line selections it's the selection's
        # start (dragging extends the word/line from its beginning).
        self._press_anchor = (self._selection.row1, self._selection.col1)
        self._refresh()

    def mousePressEvent(self, event: QMouseEvent | None) -> None:
        if event is None or self._session is None:
            return
        event.accept()
        self._click_count = self._next_click_count(event)
        self._record_press(event)
        if self._mouse_enabled():
            # The app owns the mouse: forward the press, never select.
            if event.button() in self._MOUSE_BUTTONS:
                self._mouse_drag_button = self._MOUSE_BUTTONS[event.button()]
                self._send_mouse(event, "press", self._mouse_drag_button)
                self._mouse_dragging = True
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._selection_press(event, self._click_count)
        elif event.button() == Qt.MouseButton.MiddleButton:
            self._paste()  # middle-click paste (kitty/wezterm behavior)

    def mouseDoubleClickEvent(self, event: QMouseEvent | None) -> None:
        """Qt delivers the second press of a double click as this event
        (not a press) — treat it as press #2."""
        if event is None or self._session is None:
            return
        event.accept()
        if self._mouse_enabled():
            self._mouse_drag_button = self._MOUSE_BUTTONS.get(event.button(), 0)
            self._send_mouse(event, "press", self._mouse_drag_button)
            self._mouse_dragging = True
            return
        self._click_count = 2
        self._record_press(event)
        self._selection_press(event, 2)

    def mouseMoveEvent(self, event: QMouseEvent | None) -> None:
        if event is None or self._session is None:
            return
        event.accept()
        if self._mouse_enabled():
            # `?1003` tracks every motion, `?1002` only while a button
            # is held; `?1000` alone sends no motion at all.
            if self._mouse_1003:
                self._send_mouse(event, "motion", self._mouse_drag_button)
            elif self._mouse_1002 and self._mouse_dragging:
                self._send_mouse(event, "motion", self._mouse_drag_button)
            return
        if self._mouse_dragging and self._press_anchor is not None:
            row, col = self._cell_at(event.position())
            if (row, col) == self._press_anchor:
                return  # same-cell jitter: still a click, not a drag
            self._selection = extend(
                *self._press_anchor, row, col, self._press_rectangular
            )
            self._refresh()

    def mouseReleaseEvent(self, event: QMouseEvent | None) -> None:
        if event is None or self._session is None:
            return
        event.accept()
        if self._mouse_enabled():
            if self._mouse_dragging:
                self._send_mouse(event, "release", self._mouse_drag_button)
                self._mouse_dragging = False
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._mouse_dragging = False
            self._press_anchor = None
            # A release away from the press is a drag, not a click — the
            # next press must not count as a double-click, so re-dragging
            # the same range backwards stays a fresh drag.
            if self._cell_at(event.position()) != self._cell_at(self._last_click_pos):
                self._last_click_pos = QPoint()
                self._last_click_time = 0.0

    def closeEvent(self, event: QCloseEvent | None) -> None:
        """Detach from the session before the C++ widget dies — the
        reader thread must never hand snapshots to a deleted widget."""
        self._cursor_blink_timer.stop()
        if self._session is not None and self._session.snapshot_callback is self._on_session_snapshot:
            self._session.snapshot_callback = None
        super().closeEvent(event)

    def focusInEvent(self, event: QFocusEvent | None) -> None:
        """Focus starts the blink (xterm: the cursor blinks only while
        the terminal is focused) and restores the block cursor — the
        phase re-anchors solid first, so the cursor appears solid and
        starts blinking from there."""
        super().focusInEvent(event)
        self._cursor_blink = True
        self._cursor_style = CURSOR_BLOCK
        self._repaint_cursor()
        self._cursor_blink_timer.start()

    def focusOutEvent(self, event: QFocusEvent | None) -> None:
        """Unfocused: stop the blink and freeze the cursor as a hollow
        rectangle around the cell — no flicker in the background, and
        the character underneath stays visible."""
        super().focusOutEvent(event)
        self._cursor_blink_timer.stop()
        self._cursor_blink = True
        self._cursor_style = CURSOR_OUTLINE
        self._repaint_cursor()

    def resizeEvent(self, event: QResizeEvent | None) -> None:
        super().resizeEvent(event)
        self._position_scrollbar()
        self._resize_timer.start()  # debounced → pty winsize

    def _apply_resize(self) -> None:
        if self._session is None:
            return
        # The scrollbar extent is always reserved — hidden or not — so a
        # scrollbar appearing never shrinks the grid and clips content.
        extent = self._scrollbar.sizeHint().width()
        width = max(1, self.width() - extent)
        height = max(1, self.height())
        lines = max(1, height // self._renderer.cell_h)
        columns = max(1, int(width // self._renderer.cell_w))
        if (lines, columns) == (self._lines, self._columns):
            return  # unchanged: no resize to post (spec §7)
        self._lines, self._columns = lines, columns
        self._resize_backing()
        self.update()
        self._session.resize(lines, columns)

    def focusNextPrevChild(self, nextChild: bool) -> bool:
        """Tab/Shift+Tab must reach the shell, not move focus."""
        return False


class TerminalWidget(TerminalMixin, QWidget):
    """The CPU paint backend: snapshots render into a persistent QImage
    (partial snapshots repaint only their dirty rows) and `paintEvent`
    blits it."""

    _snapshot_ready = pyqtSignal(object)  # Snapshot, delivered on the GUI thread

    def __init__(self, session: Session | None = None, parent: QWidget | None = None) -> None:
        super().__init__(session, parent)
        # No background erase: paintEvent fills everything itself.
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self._image = self._new_backing()

    # -- Backend hooks ----------------------------------------------------

    def _new_backing(self) -> QImage:
        """A fresh backing image at the widget's device-pixel ratio:
        rendered 1:1 with the physical pixels so the blit never
        upscales (a 1× raster blitted to a 2× Retina surface is
        blurry)."""
        dpr = self.devicePixelRatioF()
        image = QImage(
            max(1, round(self._columns * self._renderer.cell_w * dpr)),
            max(1, round(self._lines * self._renderer.cell_h * dpr)),
            QImage.Format.Format_RGB32,
        )
        image.setDevicePixelRatio(dpr)
        image.fill(self._renderer.default_bg)  # the terminal background, not pure black
        return image

    def _rebuild_backing(self) -> None:
        self._image = self._new_backing()

    def _rerender_full(self) -> None:
        """Render the whole frame from the merged viewport — the source
        of truth. Never from the last snapshot alone: an incremental
        one would paint only its dirty rows into the image, blanking
        every other row."""
        if self._last_snapshot is not None:
            self._renderer.render(
                self._image,
                self._last_snapshot,
                rows=self._viewport_rows,
                selection=self._selection,
                cursor_visible=self._cursor_blink,
                cursor_style=self._cursor_style,
            )

    def _refresh(self) -> None:
        """Re-render the backing with the current selection and
        repaint — the image is the source of truth (the blit only
        copies it), so a selection change must re-render it. The blink
        phase rides along (`cursor_visible` override), so a re-render
        doesn't un-blink a cursor that was mid-hidden-phase."""
        self._rerender_full()
        self.update()

    def _repaint_cursor(self) -> None:
        """Re-render only the cursor row with the current blink phase
        and cursor style — the minimal repaint: one row's raster, one
        row's update rect. Off-viewport cursors and app-hidden cursors
        (DECTCEM `?25l`) draw nothing (and the row stays as it was)."""
        snapshot = self._last_snapshot
        if snapshot is None or not snapshot.cursor_visible:
            return
        row = snapshot.cursor[0] + snapshot.viewport_offset
        if not (0 <= row < self._lines):
            return
        self._renderer.render(
            self._image,
            snapshot,
            rows=self._viewport_rows,
            row_indices=(row,),
            selection=self._selection,
            cursor_visible=self._cursor_blink,
            cursor_style=self._cursor_style,
        )
        self.update(
            QRect(
                0,
                row * self._renderer.cell_h,
                round(self._columns * self._renderer.cell_w),
                self._renderer.cell_h,
            )
        )

    def _resize_backing(self) -> None:
        self._rebuild_backing()
        # Best-effort repaint of the last state — no stale frame at the
        # old grid size while the reader resizes (the next snapshot is
        # full and replaces this).
        self._rerender_full()

    def _apply_snapshot(self, snapshot: Snapshot) -> None:
        """GUI thread: merge into the viewport rows, repaint, mirror
        the scrollbar and mode flags. `render` repaints only the
        snapshot's dirty rows into the backing image (the merged
        viewport carries the others — `row_indices` limits the
        rasterize to them, so a one-row change re-renders one row,
        not the frame); `_request_repaint` limits the paint event to
        the region they cover — partial rendering end to end. The
        selection rides along, so a re-render after mouse events shows
        it without waiting for the next snapshot. New output
        invalidates the selection first (`_clear_selection` re-renders
        its rows un-reversed — the dirty-row repaint below would leave
        the reversed cells behind, since the selection's rows are not
        necessarily dirty)."""
        if snapshot.full or snapshot.content_changed:
            self._clear_selection()  # the text under it changed (scroll rule)
        self._last_snapshot = snapshot
        self._viewport_rows = merge_viewport(snapshot, self._viewport_rows)
        self._renderer.render(
            self._image,
            snapshot,
            rows=self._viewport_rows,
            row_indices=None if snapshot.full else snapshot.dirty_rows,
            selection=self._selection,
            cursor_style=self._cursor_style,
        )
        self._mirror_flags(snapshot)
        self._request_repaint(snapshot)

    def sizeHint(self) -> QSize:
        # Logical size: the backing image is dpr-scaled, layout wants
        # logical (device-independent) pixels.
        dpr = self.devicePixelRatioF()
        return QSize(
            round(self._image.width() / dpr), round(self._image.height() / dpr)
        )

    def changeEvent(self, event: QEvent | None) -> None:
        """Rebuild the backing when the widget moves to a screen with a
        different scale — otherwise the raster would be at the old
        ratio there (blurry again)."""
        super().changeEvent(event)
        if event is not None and event.type() == QEvent.Type.DevicePixelRatioChange:
            self._rebuild_backing()
            self._rerender_full()
            self.update()

    def showEvent(self, event):
        self._apply_resize()
        super().showEvent(event)

    # -- Painting ----------------------------------------------------------

    def paintEvent(self, event: QPaintEvent | None) -> None:
        # Fill the whole dirty area with the terminal background first:
        # the grid may not tile the widget (slivers at bottom/right), and
        # WA_OpaquePaintEvent means no background erase (no flicker).
        painter = QPainter(self)
        rect = event.rect() if event is not None else self.rect()
        painter.fillRect(rect, self._renderer.default_bg)
        dpr = self.devicePixelRatioF()
        # A backing built before the widget was shown on a scaled screen
        # is 1x — rebuild it lazily rather than upscale the blit.
        dpr_mismatch = self._image.devicePixelRatio() != dpr
        if dpr_mismatch:
            self._rebuild_backing()
        grid = QRect(
            0, 0, round(self._image.width() / dpr), round(self._image.height() / dpr)
        )
        # The whole grid damaged — the backing may be stale (the
        # compositor dropped the window surface on display sleep/wake,
        # the rebuild just cleared it): re-render the frame instead of
        # blitting it. Partial repaints blit (the common path).
        if dpr_mismatch or rect.contains(grid):
            self._rerender_full()
        # Blit only the damaged region — the source rect is in the
        # image's device pixels, the target in logical coordinates, so
        # Qt maps 1:1 physical pixels (partial rendering).
        src = rect.intersected(grid)
        if not src.isEmpty():
            src_device = QRect(
                round(src.left() * dpr),
                round(src.top() * dpr),
                round(src.width() * dpr),
                round(src.height() * dpr),
            )
            painter.drawImage(src.topLeft(), self._image, src_device)
        painter.end()
