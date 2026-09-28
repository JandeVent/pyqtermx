# -*- coding: utf-8 -*-
# Copyright (C) 2018-2026 Connet Information Technology Company, Shanghai.
"""Phase 5 — Windows pty (ADR-0007): the WinPty wrapper over pywinpty's
ConPTY, pinned to the same contract as `pyqtermx.ptyspawn.Pty`.

Two layers: mock-based tests that run on any platform (a
`FakeWinPtyProcess` stands in for pywinpty, so the wrapper's contract —
read/EOF mapping, UTF-8 roundtrip, env, close escalation — is pinned
everywhere), and integration tests that spawn a real ConPTY child on
Windows (skipped elsewhere, where Windows CI runs them).
"""

from __future__ import annotations

import os
import select
import signal
import sys
import time

import pytest

from pyqtermx.session import Session
from pyqtermx.win_pty import WinPty

from tests.pty.test_pty import wait_for


class FakeWinPtyProcess:
    """A pywinpty PtyProcess stand-in: a scripted ConPTY child. Output
    the test "spawns" with lands in reads; written data lands in
    `written`; geometry lands in `winsizes`."""

    def __init__(self) -> None:
        self.pid = 4242
        self.output_queue: list[str] = []
        self.written: list[str] = []
        self.winsizes: list[tuple[int, int]] = []
        self.alive = True
        self.closed = False
        self._exitstatus: int | None = None
        self.spawn_args: tuple | None = None

    @classmethod
    def spawn(cls, argv, cwd=None, env=None, dimensions=None) -> "FakeWinPtyProcess":
        inst = cls()
        inst.spawn_args = (list(argv), cwd, dict(env or {}), dimensions)
        return inst

    # -- the pywinpty surface -------------------------------------------

    def isalive(self) -> bool:
        return self.alive and not self.closed

    def read(self, size: int = 1024) -> str:
        if self.output_queue:
            return self.output_queue.pop(0)
        if not self.isalive():
            raise EOFError("Pty is closed")
        return ""

    def write(self, s: str) -> int:
        self.written.append(s)
        return len(s)

    def setwinsize(self, rows: int, cols: int) -> None:
        self.winsizes.append((rows, cols))

    def close(self, force: bool = False) -> None:
        self.closed = True

    def terminate(self, force: bool = False) -> bool | None:
        self.alive = False
        self._exitstatus = 1
        return True

    def fileno(self) -> int:
        return 99

    @property
    def exitstatus(self) -> int | None:
        return self._exitstatus if not self.isalive() else None


@pytest.fixture
def fake_backend(monkeypatch) -> type[FakeWinPtyProcess]:
    """Install the fake pywinpty and return its class."""
    monkeypatch.setattr("pyqtermx.win_pty._WinPtyProcess", FakeWinPtyProcess)
    return FakeWinPtyProcess


def spawn_fake(fake_backend, command=None, **kwargs) -> tuple[WinPty, FakeWinPtyProcess]:
    pty = WinPty(command, **kwargs)
    assert pty._pty is not None
    return pty, pty._pty


# -- Spawn ---------------------------------------------------------------


def test_spawn_passes_command_env_and_geometry(fake_backend) -> None:
    pty, fake = spawn_fake(
        fake_backend,
        ["prog.exe", "arg"],
        env={"FOO": "bar", "COLUMNS": "999", "LINES": "999"},
        rows=30,
        cols=100,
    )
    argv, cwd, env, dimensions = fake.spawn_args
    assert argv == ["prog.exe", "arg"]
    assert env["FOO"] == "bar"
    assert env["TERM"] == "xterm-256color"
    assert env["COLORTERM"] == "truecolor"
    # COLUMNS/LINES must NOT reach the child — even stale values from
    # the supplied env (see the rationale in win_pty.WinPty.__init__).
    assert "COLUMNS" not in env
    assert "LINES" not in env
    assert dimensions == (30, 100)
    assert pty.pid == 4242
    assert pty.is_running()


def test_spawn_without_command_uses_cmd(fake_backend) -> None:
    pty, fake = spawn_fake(fake_backend, None)
    argv, _, _, _ = fake.spawn_args
    assert argv == ["cmd.exe"]


def test_spawn_failure_reports_127(monkeypatch) -> None:
    class FailingSpawn:
        @classmethod
        def spawn(cls, argv, cwd=None, env=None, dimensions=None):
            raise FileNotFoundError("no such program")

    monkeypatch.setattr("pyqtermx.win_pty._WinPtyProcess", FailingSpawn)
    pty = WinPty(["missing.exe"])
    assert not pty.is_running()
    assert pty.wait() == 127  # the Unix exec-failure exit code
    assert pty.read() is None
    assert pty.master_fd == -1
    pty.close()  # no-op, must not raise


# -- Read / write --------------------------------------------------------


def test_read_returns_bytes_and_none_at_eof(fake_backend) -> None:
    pty, fake = spawn_fake(fake_backend, ["echo.exe"])
    fake.output_queue = ["hello", "wörld"]
    assert pty.read() == b"hello"
    assert pty.read() == "wörld".encode()  # UTF-8 roundtrip
    assert pty.read() == b""  # alive, nothing queued
    fake.alive = False
    assert pty.read() is None  # EOFError → the "child is gone" signal


def test_read_none_when_child_died_before_eof(fake_backend) -> None:
    pty, fake = spawn_fake(fake_backend, ["echo.exe"])
    fake.output_queue = [""]  # pump delivered nothing
    fake.alive = False
    assert pty.read() is None


def test_send_data_decodes_bytes_to_str(fake_backend) -> None:
    pty, fake = spawn_fake(fake_backend, ["cat.exe"])
    pty.send_data("pîng\r".encode())
    assert fake.written == ["pîng\r"]


def test_send_data_is_noop_when_child_is_dead(fake_backend) -> None:
    pty, fake = spawn_fake(fake_backend, ["cat.exe"])
    fake.alive = False
    pty.send_data(b"x")
    assert fake.written == []


# -- Geometry ------------------------------------------------------------


def test_set_window_size_forwards_to_conpty(fake_backend) -> None:
    pty, fake = spawn_fake(fake_backend, ["prog.exe"])
    pty.set_window_size(33, 120)
    assert fake.winsizes == [(33, 120)]
    assert pty.rows == 33
    assert pty.cols == 120


# -- Lifecycle -----------------------------------------------------------


def test_wait_is_none_while_running_then_exit_status(fake_backend) -> None:
    pty, fake = spawn_fake(fake_backend, ["prog.exe"])
    assert pty.is_running()
    assert pty.wait() is None
    fake.alive = False
    fake._exitstatus = 0
    assert not pty.is_running()
    assert pty.wait() == 0


def test_signal_maps_to_terminate_process(monkeypatch, fake_backend) -> None:
    pty, _ = spawn_fake(fake_backend, ["prog.exe"])
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr("pyqtermx.win_pty.os.kill", lambda pid, sig: killed.append((pid, sig)))
    pty.signal(signal.SIGTERM)
    assert killed == [(4242, signal.SIGTERM)]
    pty.signal(signal.SIGINT)  # no POSIX signals on Windows — no-op
    assert killed == [(4242, signal.SIGTERM)]


def test_close_terminates_and_reaps(fake_backend) -> None:
    pty, fake = spawn_fake(fake_backend, ["sleep.exe"])
    pty.close(terminate_timeout=0.2, kill_timeout=0.2)
    assert fake.closed
    assert not pty.is_running()
    assert pty.wait() == 0


def test_close_escalates_when_child_stubborn(monkeypatch, fake_backend) -> None:
    class StubbornFake(FakeWinPtyProcess):
        def close(self, force: bool = False) -> None:
            # The child ignores EOF — stay alive.
            self.close_force = force

        def terminate(self, force: bool = False) -> bool | None:
            self.terminate_force = force
            self.alive = False
            self._exitstatus = 1
            return True

    monkeypatch.setattr("pyqtermx.win_pty._WinPtyProcess", StubbornFake)
    pty = WinPty(["stubborn.exe"])
    assert pty._pty is not None
    stubborn = pty._pty
    pty.close(terminate_timeout=0.05, kill_timeout=0.05)
    # EOF first (force=False), then the forced terminate as the kill.
    assert stubborn.close_force is False
    assert stubborn.terminate_force is True
    assert pty.wait() == 1


def test_close_is_idempotent(fake_backend) -> None:
    pty, _ = spawn_fake(fake_backend, ["prog.exe"])
    pty.close()
    pty.close()  # must not raise


# -- Session integration (the reader loop's contract) --------------------


def test_failed_spawn_ends_session_loop_cleanly(monkeypatch) -> None:
    """A spawn failure leaves a dead pty (exit 127); the session's
    reader loop must end gracefully instead of crashing on the -1 fd."""
    class FailingSpawn:
        @classmethod
        def spawn(cls, argv, cwd=None, env=None, dimensions=None):
            raise FileNotFoundError("no such program")

    monkeypatch.setattr("pyqtermx.win_pty._WinPtyProcess", FailingSpawn)
    pty = WinPty(["missing.exe"])
    session = Session(pty, lines=5, columns=20)
    session.start()
    assert wait_for(lambda: not session.is_alive, timeout=5.0)
    session.close()  # no-op, must not raise


# -- Real ConPTY (Windows only) ------------------------------------------

win32 = pytest.mark.skipif(sys.platform != "win32", reason="ConPTY is Windows-only")


def read_until(pty: WinPty, marker: bytes, timeout: float = 8.0) -> bytes:
    """Read from the pty until `marker` appears; return everything read."""
    out = b""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready, _, _ = select.select([pty.master_fd], [], [], 0.05)
        if ready:
            chunk = pty.read()
            if chunk is None:
                break
            out += chunk
            if marker in out:
                return out
    return out


@win32
def test_real_conpty_spawn_echoes() -> None:
    pty = WinPty([sys.executable, "-c", "print('READY', flush=True)"])
    try:
        out = read_until(pty, b"READY")
        assert b"READY" in out
    finally:
        pty.close()
    assert not pty.is_running()
    assert pty.wait() == 0


@win32
def test_real_conpty_send_data_reaches_child() -> None:
    pty = WinPty(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "print('READY', flush=True)\n"
            "line = sys.stdin.readline()\n"
            "print('GOT:' + line.strip(), flush=True)\n",
        ]
    )
    try:
        read_until(pty, b"READY")
        pty.send_data(b"ping\r")
        assert b"GOT:ping" in read_until(pty, b"GOT:ping")
    finally:
        pty.close()


@win32
def test_real_conpty_close_terminates_child() -> None:
    pty = WinPty([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        read_until(pty, b"", timeout=2.0)
        assert pty.is_running()
    finally:
        pty.close(terminate_timeout=0.5, kill_timeout=1.0)
    assert not pty.is_running()
