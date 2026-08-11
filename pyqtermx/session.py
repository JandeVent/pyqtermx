# -*- coding: utf-8 -*-
# Copyright (C) 2018-2026 Connet Information Technology Company, Shanghai.
"""The session — reader thread, command queue, snapshots (ADR-0005).

The single writer of terminal state. A dedicated reader thread owns the
emulator and the screen: it reads the pty and applies every command from
the queue (`send_data`, `resize`, `scroll`, `scroll_to_bottom`, `close`)
in arrival order, serialized with output reads. The GUI thread never
reads or writes the model — it renders from snapshots and posts
commands. Qt-free: the GUI layer (Slice B) bridges `Snapshot` delivery
to queued signals.
"""

from __future__ import annotations

import queue
import select
import threading
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from .emulator import Emulator
from .parser import Parser
from .screen import DECTCEM, Row, Screen


class PtyLike(Protocol):
    """The narrow pty surface the reader thread drives."""

    @property
    def master_fd(self) -> int: ...

    def read(self) -> bytes | None: ...

    def send_data(self, data: bytes) -> None: ...

    def set_window_size(self, rows: int, cols: int) -> None: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class Snapshot:
    """What the GUI renders from — immutable, handoff-race-free.

    - `dirty_rows`: viewport-row indices whose content changed (empty
      when `full`); `rows` holds their frozen `Row`s at the same indices.
    - `scrollback_len` / `viewport_offset`: the scrollbar's range and
      position (ADR-0006).
    - `cursor`: (row, col) in grid coordinates; the GUI maps it through
      the offset.
    - `cursor_visible` / `cursor_color`: the cursor is not hidden
      (DECTCEM `?25`, shown by default) — the GUI skips the block when
      false — and the OSC 12 cursor color (`#rrggbb`, None = the
      default inverted block) the renderer paints the cursor with.
    - `dec_ckm` / `bracketed_paste` / `reverse_video`: the input-path and
      rendition mode flags the GUI needs (Q8, effective_rendition) — it
      cannot read the model. `reverse_video` (`?5`) is a *visible* mode:
      a change forces a full repaint.
    - `mouse_1000` / `mouse_1002` / `mouse_1003` / `mouse_1006`: the
      mouse-tracking modes (`?1000` X10, `?1002` button-event, `?1003`
      any-event, `?1006` SGR) — the widget routes clicks and wheel to
      the child when any is set, and picks the encoding by `?1006`.
    - `alt_screen`: the alternate screen is active (`?1049`) — no
      scrollback exists there (ADR-0006), so the wheel becomes
      Up/Down arrows to the app (line-by-line cursor moves).
    - `full`: the whole viewport must repaint (initial state, resize,
      offset change, `?5` change, ED3).
    - `content_changed`: cell content actually changed — the grid had
      dirty rows before the cursor's old/new rows were added for its
      repaint. A cursor move alone repaints rows without changing text;
      the widget uses the flag to clear a selection only when the text
      under it changed (the same rule as scrolling, ADR-0005).
    """

    dirty_rows: tuple[int, ...]
    rows: tuple[Row, ...]
    scrollback_len: int
    viewport_offset: int
    cursor: tuple[int, int]
    dec_ckm: bool = False
    bracketed_paste: bool = False
    reverse_video: bool = False
    mouse_1000: bool = False
    mouse_1002: bool = False
    mouse_1003: bool = False
    mouse_1006: bool = False
    alt_screen: bool = False
    full: bool = False
    content_changed: bool = False
    cursor_visible: bool = True
    cursor_color: str | None = None


class Session:
    """The headless single-writer core: pty + parser + screen + thread.

    Commands are posted from any thread; snapshots are delivered from
    the reader thread via the optional `snapshot_callback` and appended
    to `snapshots` (the test seam).
    """

    def __init__(
        self,
        pty: PtyLike,
        *,
        lines: int = 24,
        columns: int = 80,
        scrollback_limit: int = 1000,
        snapshot_callback: Callable[[Snapshot], None] | None = None,
    ) -> None:
        self.pty = pty
        self.lines = lines
        self.columns = columns
        self.screen = Screen(lines=lines, columns=columns, scrollback_limit=scrollback_limit)
        # The reply callback runs on the reader thread — the single
        # writer — so it can drive the pty like every other write.
        self.emulator = Emulator(
            self.screen, reply=lambda text: self.pty.send_data(text.encode("utf-8"))
        )
        self.parser = Parser(self.emulator)
        self.snapshots: list[Snapshot] = []
        self._callback = snapshot_callback
        self._queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._full = True
        self._last_offset = 0
        self._last_cursor = (0, 0)
        self._last_modes = (False, False, False, False, False, False)
        self._last_reverse = False
        self._last_cursor_visible = True
        self._last_cursor_color: str | None = None
        self._thread = threading.Thread(target=self._run, name="pyqtermx-reader", daemon=True)

    # -- Command API (any thread) ---------------------------------------

    @property
    def snapshot_callback(self) -> Callable[[Snapshot], None] | None:
        return self._callback

    @snapshot_callback.setter
    def snapshot_callback(self, callback: Callable[[Snapshot], None] | None) -> None:
        """Attach a snapshot consumer after construction (the GUI bridge
        wires itself to an already-running session)."""
        self._callback = callback

    def send_data(self, data: bytes) -> None:
        self._queue.put(("send", data))

    def resize(self, lines: int, columns: int) -> None:
        self._queue.put(("resize", (lines, columns)))

    def scroll(self, n: int) -> None:
        self._queue.put(("scroll", n))

    def scroll_to_bottom(self) -> None:
        self._queue.put(("scroll_to_bottom", None))

    def set_palette(self, fg: str, bg: str) -> None:
        """Replace the default foreground/background colors the
        emulator reports to OSC 10/11 color queries (hex `#rrggbb` —
        the `QColor.name(HexRgb)` form). Posted to the reader thread
        like every command, so theme detection by TUI apps in the
        child reports the themed colors."""
        self._queue.put(("palette", (fg, bg)))

    def process(self, data: bytes) -> None:
        """Run the reader thread's per-read step synchronously: feed the
        data into the parser, flush the write boundary, emit a snapshot.
        The thread's loop is `select` on the pty + this call — driving
        it directly is the benchmark seam (bytes → Snapshot, no thread).

        Chunking invariant (T6): feeding in pty-sized chunks produces
        exactly the same result as one big feed — the conformance
        harness and the bench `_feed` both rely on it."""
        self.parser.feed_bytes(data)
        self.parser.flush()
        self._emit()

    def close(self, timeout: float = 5.0) -> None:
        """Close the pty and stop the reader thread (idempotent). The
        reader applies the close command itself; join() waits for it."""
        self._queue.put(("close", None))
        self._thread.join(timeout)

    # -- Reader thread --------------------------------------------------

    def start(self) -> None:
        self._thread.start()

    @property
    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def _run(self) -> None:
        try:
            self._emit()  # the initial full snapshot (blank screen)
            while True:
                if not self._drain_commands():
                    return
                try:
                    ready, _, _ = select.select([self.pty.master_fd], [], [], 0.05)
                except (OSError, ValueError):
                    return  # pty closed under us, or never existed
                if ready:
                    data = self.pty.read()
                    if data is None:
                        # Child exited — emit the final state and stop.
                        self._emit()
                        return
                    if data:
                        self.process(data)
                self._emit()
        finally:
            self.pty.close()

    def _drain_commands(self) -> bool:
        """Apply every queued command in arrival order. False once a
        `close` was applied (the loop must stop)."""
        while True:
            try:
                kind, payload = self._queue.get_nowait()
            except queue.Empty:
                return True
            if kind == "close":
                return False
            if kind == "send":
                self.pty.send_data(payload)
            elif kind == "resize":
                lines, columns = payload
                self.screen.resize(lines, columns)
                self._full = True
                self.pty.set_window_size(lines, columns)
            elif kind == "scroll":
                self.screen.scroll(payload)
            elif kind == "scroll_to_bottom":
                self.screen.scroll_to_bottom()
            elif kind == "palette":
                fg, bg = payload
                self.emulator.set_palette(fg, bg)

    def _emit(self) -> None:
        """Emit a snapshot when anything visible changed. Rows are
        frozen objects — the handoff needs no locks and no copies."""
        screen = self.screen
        dirty = screen.take_dirty_rows()
        # True when the grid itself changed — before the cursor's
        # repaint rows join `dirty`: a cursor move repaints rows
        # without changing text, and the selection must survive that.
        content_changed = bool(dirty)
        offset = screen.viewport_offset
        cursor = (screen.cursor.y, screen.cursor.x)
        modes = (
            screen.mode(1, private=True),
            screen.mode(2004, private=True),
            screen.mode(1000, private=True),
            screen.mode(1002, private=True),
            screen.mode(1003, private=True),
            screen.mode(1006, private=True),
        )
        reverse = screen.mode(5, private=True)  # DECSCNM — a visible mode
        cursor_visible = screen.mode(DECTCEM, private=True)  # DECTCEM — a visible mode
        cursor_color = self.emulator.cursor_color
        full = self._full or offset != self._last_offset or reverse != self._last_reverse
        mode_changed = modes != self._last_modes
        if cursor != self._last_cursor:
            # A cursor move repaints its old and new rows.
            dirty.add(self._last_cursor[0])
            dirty.add(cursor[0])
        if cursor_visible != self._last_cursor_visible:
            # A visibility flip repaints the cursor row too — the block
            # is painted over the row, so hiding it must repaint the row
            # (like a cursor move: no text changed, the selection lives).
            dirty.add(self._last_cursor[0])
            dirty.add(cursor[0])
        if cursor_color != self._last_cursor_color:
            # An OSC 12 color change repaints the cursor row too — the
            # block color is visible state, like a visibility flip.
            dirty.add(self._last_cursor[0])
            dirty.add(cursor[0])
        if not full and not dirty and not mode_changed:
            return
        if full:
            # Initial state, resize, offset change, ED3: everything.
            vrows = tuple(range(screen.lines))
            rows = tuple(screen.viewport_row(k) for k in vrows)
        elif dirty:
            # A dirty grid row `y` shows at viewport row `y + offset`;
            # rows scrolled off the viewport need no repaint.
            vrows = tuple(sorted({k for y in dirty if (k := y + offset) < screen.lines}))
            if not vrows and not mode_changed:
                return
            rows = tuple(screen.viewport_row(k) for k in vrows)
        else:
            # A mode change alone: the GUI updates its input mirror.
            vrows = ()
            rows = ()
        snapshot = Snapshot(
            # Full repaints ignore the per-row list — the flag says it all.
            dirty_rows=() if full else vrows,
            rows=rows,
            scrollback_len=screen.scrollback_len,
            viewport_offset=offset,
            cursor=cursor,
            dec_ckm=modes[0],
            bracketed_paste=modes[1],
            mouse_1000=modes[2],
            mouse_1002=modes[3],
            mouse_1003=modes[4],
            mouse_1006=modes[5],
            alt_screen=screen.alt_screen,
            reverse_video=reverse,
            full=full,
            content_changed=content_changed,
            cursor_visible=cursor_visible,
            cursor_color=cursor_color,
        )
        self.snapshots.append(snapshot)
        if self._callback is not None:
            self._callback(snapshot)
        self._last_offset = offset
        self._last_cursor = cursor
        self._last_modes = modes
        self._last_reverse = reverse
        self._last_cursor_visible = cursor_visible
        self._last_cursor_color = cursor_color
        self._full = False
