# -*- coding: utf-8 -*-
# Copyright (C) 2018-2026 Connet Information Technology Company, Shanghai.
"""The Windows pty layer — Qt-free, ConPTY-backed (ADR-0007).

A `WinPty` implements the same narrow interface as
`pyqtermx.ptyspawn.Pty` (ADR-0005): spawn a child behind a real
pseudo-terminal, exchange bytes, set the window size, and reap the
exit. The child lives in a ConPTY created by pywinpty (the Rust
winpty-rs wrapper); pywinpty's PtyProcess already pumps the ConPTY
pipe through a loopback TCP socket pair, so `master_fd` is a *socket*
and the session's select()-driven reader thread works unchanged
(sockets are the only selectable things on Windows).

Three ConPTY differences are absorbed here, so consumers see the Unix
contract:

- Output is UTF-8 — pywinpty decodes it to str; the interface
  re-encodes to bytes (errors='replace', as the reference
  implementation does).
- EOF surfaces as EOFError from pywinpty's pump — mapped to None, the
  same "child is gone" signal as the Unix pty.
- There are no POSIX signals: SIGTERM maps to TerminateProcess, the
  console owns Ctrl+C, and close() escalates to a forced terminate
  (the Windows kill-equivalent).

The module imports on every platform — the winpty package itself is
guarded, so macOS/Linux builds and the mock tests are unaffected.
"""

from __future__ import annotations

import os
import signal
import sys
import time
from typing import Mapping, Protocol, Sequence

#: The terminal type the child sees — mirrors ptyspawn.DEFAULT_TERM
#: (ptyspawn cannot be imported on Windows: fcntl/termios/pty are
#: Unix-only).
DEFAULT_TERM = "xterm-256color"

#: Truecolor advertisement — mirrors ptyspawn.COLORTERM (apps gate
#: `38;2` output on it; TERM alone does not advertise RGB support).
COLORTERM = "truecolor"


class _WinPtyProto(Protocol):
    """The pywinpty PtyProcess surface WinPty drives — a structural
    type, so the wrapper type-checks on every platform without
    importing the Windows-only package."""

    pid: int | None

    @classmethod
    def spawn(
        cls,
        argv: Sequence[str],
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        dimensions: tuple[int, int] | None = None,
    ) -> _WinPtyProto: ...

    def isalive(self) -> bool: ...
    def read(self, size: int = 1024) -> str: ...
    def write(self, s: str) -> int: ...
    def setwinsize(self, rows: int, cols: int) -> None: ...
    def close(self, force: bool = False) -> None: ...
    def terminate(self, force: bool = False) -> bool | None: ...
    def fileno(self) -> int: ...

    @property
    def exitstatus(self) -> int | None: ...


_WinPtyProcess: type[_WinPtyProto] | None
if sys.platform == "win32":
    from winpty import PtyProcess as _WinPtyProcess  # type: ignore[import-not-found]
else:
    _WinPtyProcess = None


class WinPty:
    """A ConPTY pseudo-terminal pair with a spawned child.

    The Windows sibling of `pyqtermx.ptyspawn.Pty` (ADR-0005):
    construct to spawn, exchange bytes through `read`/`send_data`,
    resize with `set_window_size`, poll the exit with
    `is_running`/`wait`, and `close`. Qt-free.
    """

    def __init__(
        self,
        command: Sequence[str] | None = None,
        *,
        env: Mapping[str, str] | None = None,
        rows: int = 24,
        cols: int = 80,
    ) -> None:
        if _WinPtyProcess is None:
            raise RuntimeError("pywinpty is required on Windows (pip install pywinpty)")
        self.rows = rows
        self.cols = cols
        self.pid: int | None = None
        self._pty: _WinPtyProto | None = None
        self._closed = False
        self._exit_status: int | None = None

        cmd = list(command) if command is not None else ["cmd.exe"]
        child_env = dict(os.environ)
        if env:
            child_env.update(env)
        # The child always sees a compatible TERM, exactly like the
        # Unix path (spec: TUIs behave differently). COLORTERM
        # advertises truecolor support (38;2/48;2). COLUMNS/LINES must
        # NOT be set (and any inherited values are stripped): Python
        # 3.14's shutil.get_terminal_size() — the size source of
        # Textual and other TUIs — prefers them over the console size,
        # and they are frozen at spawn, so a resize would never reach
        # the app. ConPTY's dimensions are authoritative.
        child_env["TERM"] = DEFAULT_TERM
        child_env["COLORTERM"] = COLORTERM
        child_env.pop("COLUMNS", None)
        child_env.pop("LINES", None)

        try:
            self._pty = _WinPtyProcess.spawn(
                cmd,
                cwd=os.getcwd(),
                env=child_env,
                dimensions=(rows, cols),
            )
        except OSError:
            # The child never started (missing program, bad cwd...).
            # The Unix path reports the same failure as exit 127 (the
            # child's exec failure) — mirror it so consumers see a
            # dead session instead of a live one.
            self._exit_status = 127
            return
        self.pid = self._pty.pid

    # -- Read API --------------------------------------------------------

    @property
    def master_fd(self) -> int:
        """The select() surface: pywinpty's loopback socket fd. -1 when
        the spawn failed — the reader loop's select errors and ends
        (session.py treats that as "pty closed under us")."""
        if self._pty is None:
            return -1
        return self._pty.fileno()

    def read(self) -> bytes | None:
        """One read of the child's output. None when the child has
        exited or the pty is closed (pywinpty raises EOFError once its
        pump hits the ConPTY EOF); b"" when no data arrived."""
        if self._closed or self._pty is None:
            return None
        try:
            text = self._pty.read(65536)
        except (EOFError, OSError):
            return None  # the pump is gone — so is the child
        if text == "" and not self._pty.isalive():
            # The child is gone but the pump has not closed the socket
            # yet — the same "child exited" signal as EOFError.
            return None
        return text.encode("utf-8", errors="replace")

    def send_data(self, data: bytes) -> None:
        """Write bytes to the child (its stdin). pywinpty takes str —
        decode UTF-8 with replacement (the reference implementation's
        choice). A dead child is a no-op."""
        if self._closed or self._pty is None or not self._pty.isalive():
            return
        try:
            self._pty.write(data.decode("utf-8", errors="replace"))
        except (EOFError, OSError):
            pass

    # -- Size ------------------------------------------------------------

    def set_window_size(self, rows: int, cols: int) -> None:
        """The size the child sees: ConPTY forwards it to the child's
        console — the Windows analogue of TIOCSWINSZ + SIGWINCH."""
        self.rows = rows
        self.cols = cols
        if self._pty is not None and self._pty.isalive():
            try:
                self._pty.setwinsize(rows, cols)
            except (EOFError, OSError):
                pass

    # -- Lifecycle -------------------------------------------------------

    def is_running(self) -> bool:
        """Whether the child is still alive."""
        return self._pty is not None and self._pty.isalive()

    def wait(self) -> int | None:
        """The child's exit status, or None while it is still running.
        Never blocks — callers poll."""
        if self._exit_status is not None:
            return self._exit_status
        if self._pty is not None and not self._pty.isalive():
            status = self._pty.exitstatus
            self._exit_status = status if status is not None else 0
        return self._exit_status

    def signal(self, sig: int) -> None:
        """Send a signal to the child. Windows has no POSIX signals:
        SIGTERM maps to TerminateProcess; everything else is a no-op
        (Ctrl+C lives in the console, not in kill())."""
        if sig not in (signal.SIGTERM, signal.SIGKILL) or self.pid is None:
            return
        try:
            os.kill(self.pid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass

    def close(self, terminate_timeout: float = 3.0, kill_timeout: float = 2.0) -> None:
        """Close the pty and stop the child (spec US 21).

        Closing first delivers EOF to a well-behaved child (the normal
        terminal way); a child still alive afterwards is terminated,
        with a bounded wait, then a forced terminate — Windows has no
        SIGKILL, so pywinpty's forced terminate (SIGINT then SIGTERM)
        is the kill-equivalent."""
        if self._pty is None:
            self._closed = True
            return
        if not self._closed:
            self._closed = True
            try:
                self._pty.close(force=False)  # EOF, then terminate
            except (EOFError, OSError):
                pass
        if self._pty.isalive():
            self._wait_bounded(terminate_timeout)
        if self._pty.isalive():
            try:
                self._pty.terminate(force=True)
            except (EOFError, OSError):
                pass
            self._wait_bounded(kill_timeout)
        self.wait()

    def _wait_bounded(self, timeout: float) -> None:
        """Poll until the child exits or `timeout` passes."""
        deadline = time.monotonic() + timeout
        while (
            self._pty is not None
            and self._pty.isalive()
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
