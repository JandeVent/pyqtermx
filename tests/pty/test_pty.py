# -*- coding: utf-8 -*-
# Copyright (C) 2018-2026 Connet Information Technology Company, Shanghai.
"""Phase 4 — Pty interface (ADR-0005): spawn a child with its own
session, exchange bytes, set the window size, reap the exit.

The seam: the narrow Qt-free `Pty` interface, driven against a fake
child program (a python -c script) through a real pty pair. Asserts are
tolerant: a wait_for(predicate, timeout) polling helper, no brittle
sleeps.
"""

from __future__ import annotations

import errno
import os
import select
import signal
import sys
import tempfile
import time

import pytest

from pyqtermx.ptyspawn import Pty


def wait_for(predicate, timeout: float = 5.0) -> bool:
    """Poll `predicate` until it is truthy or `timeout` seconds pass."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def read_until(pty: Pty, marker: bytes, timeout: float = 5.0) -> bytes:
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


CHILD_ECHO = (
    "import os, ioctl, sys\n"
    "print('READY', flush=True)\n"
    "line = sys.stdin.readline()\n"
    "print('ECHO:' + line.strip(), flush=True)\n"
    "print('TERM=' + os.environ.get('TERM', ''), flush=True)\n"
    "ws = ioctl(1, 0x5413, b'\\x00' * 8)\n"  # TIOCGWINSZ on Linux
    "import array\n"
    "print('SIZE', flush=True)\n"
)


def spawn_child(script: str, **kwargs) -> Pty:
    """Spawn a fake child running `script` with python."""
    return Pty([sys.executable, "-c", script], **kwargs)


def test_spawn_runs_child_and_reads_output() -> None:
    pty = spawn_child("print('READY', flush=True)")
    try:
        out = read_until(pty, b"READY")
        assert b"READY" in out
    finally:
        pty.close()


def test_send_data_reaches_child() -> None:
    pty = spawn_child(
        "import sys\n"
        "print('READY', flush=True)\n"
        "line = sys.stdin.readline()\n"
        "print('GOT:' + line.strip(), flush=True)\n"
    )
    try:
        read_until(pty, b"READY")
        pty.send_data(b"ping\r")
        out = read_until(pty, b"GOT:ping")
        assert b"GOT:ping" in out
    finally:
        pty.close()


def test_set_window_size_reaches_child() -> None:
    pty = spawn_child(
        "import fcntl, struct, sys, termios, time\n"
        "print('READY', flush=True)\n"
        "# Poll until the parent's resize lands (no brittle sleep).\n"
        "size = (0, 0, 0, 0)\n"
        "deadline = time.monotonic() + 3.0\n"
        "while time.monotonic() < deadline:\n"
        "    size = struct.unpack('HHHH', fcntl.ioctl(0, termios.TIOCGWINSZ, b'\\x00' * 8))\n"
        "    if size[1] == 100:\n"
        "        break\n"
        "    time.sleep(0.01)\n"
        "print('SIZE:%dx%d' % (size[1], size[0]), flush=True)\n"
    )
    try:
        read_until(pty, b"READY")
        pty.set_window_size(30, 100)
        out = read_until(pty, b"SIZE:100x30")
        assert b"SIZE:100x30" in out
    finally:
        pty.close()


def test_child_exit_is_detected_and_reaped() -> None:
    pty = spawn_child("print('BYE', flush=True)\n")
    try:
        out = read_until(pty, b"BYE")
        assert b"BYE" in out
        assert wait_for(lambda: pty.read() is None)
        assert not pty.is_running()
        assert pty.wait() == 0  # clean exit reaped, no zombie
    finally:
        pty.close()


def test_child_gets_term_environment() -> None:
    pty = spawn_child(
        "import os\nprint('TERM=' + os.environ.get('TERM', ''), flush=True)\n"
    )
    try:
        out = read_until(pty, b"TERM=")
        assert b"TERM=xterm-256color" in out
    finally:
        pty.close()


def test_child_gets_colorterm_truecolor() -> None:
    """The child sees COLORTERM=truecolor so truecolor-gated apps
    (vim termguicolors, fish, git-delta…) emit 38;2/48;2 sequences."""
    pty = spawn_child(
        "import os\n"
        "print('COLORTERM=' + os.environ.get('COLORTERM', ''), flush=True)\n"
    )
    try:
        out = read_until(pty, b"COLORTERM=")
        assert b"COLORTERM=truecolor" in out
    finally:
        pty.close()


def test_child_gets_geometry_environment() -> None:
    pty = spawn_child(
        "import os\n"
        "print('GEOM:%sx%s' % (os.environ.get('COLUMNS', ''),"
        " os.environ.get('LINES', '')), flush=True)\n",
        rows=33,
        cols=120,
    )
    try:
        out = read_until(pty, b"GEOM:")
        assert b"GEOM:120x33" in out
    finally:
        pty.close()


def test_child_starts_in_cwd() -> None:
    """The child chdirs into `cwd` before exec, so the shell lands in
    the requested working directory (xCode's "Open Terminal Here")."""
    with tempfile.TemporaryDirectory() as folder:
        marker = os.path.join(folder, "cwd-ok")
        pty = spawn_child(
            "import os\n"
            f"open({marker!r}, 'w').close()\n"
            "print('CWD_DONE', flush=True)\n",
            cwd=folder,
        )
        try:
            out = read_until(pty, b"CWD_DONE")
            assert b"CWD_DONE" in out
            assert os.path.isfile(marker)
        finally:
            pty.close()


def test_close_terminates_a_still_running_child() -> None:
    pty = spawn_child("import time\ntime.sleep(60)\n")
    try:
        read_until(pty, b"", timeout=1.0)
        assert pty.is_running()
    finally:
        pty.close()
    # US 21: close() stops a still-running child — SIGHUP/EOF first,
    # then SIGTERM — and reaps it (no zombie left behind).
    assert wait_for(lambda: pty.wait() is not None)
    assert not pty.is_running()


def test_has_foreground_job_false_at_idle_prompt() -> None:
    """A shell sitting at the prompt has no foreground job — closing
    the tab should not prompt (xCode's close-confirmation rule)."""
    pty = spawn_child(
        "import os, sys\n"
        "print('IDLE', flush=True)\n"
        "sys.stdin.readline()\n"
        "print('DONE', flush=True)\n"
    )
    try:
        read_until(pty, b"IDLE")
        assert pty.has_foreground_job() is False
    finally:
        pty.close()


def test_has_foreground_job_true_while_job_runs() -> None:
    """A foreground job (the shell foregrounds a child via tcsetpgrp)
    is detected — closing the tab should prompt."""
    pty = spawn_child(
        "import os, sys, time\n"
        "print('IDLE', flush=True)\n"
        "sys.stdin.readline()\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    os.setpgid(0, 0)\n"
        "    time.sleep(30)\n"
        "    os._exit(0)\n"
        "os.setpgid(pid, pid)\n"
        "os.tcsetpgrp(0, pid)\n"  # the shell foregrounds the job
        "os.waitpid(pid, 0)\n"
        "print('DONE', flush=True)\n"
    )
    try:
        read_until(pty, b"IDLE")
        assert pty.has_foreground_job() is False
        pty.send_data(b"go\n")
        # the shell foregrounds the job — now detectable
        assert wait_for(lambda: pty.has_foreground_job())
    finally:
        pty.close()


def test_foreground_program_none_at_idle_prompt() -> None:
    """No foreground program at the idle prompt — the shell owns the
    terminal, so the panel shows no program name."""
    pty = spawn_child(
        "import os, sys\n"
        "print('IDLE', flush=True)\n"
        "sys.stdin.readline()\n"
        "print('DONE', flush=True)\n"
    )
    try:
        read_until(pty, b"IDLE")
        assert pty.foreground_program() is None
    finally:
        pty.close()


def test_foreground_program_reports_job_name_while_running() -> None:
    """A foreground job is reported by its program name — the panel
    shows 'sleep' while the job runs (mirrors has_foreground_job)."""
    pty = spawn_child(
        "import os, sys\n"
        "print('IDLE', flush=True)\n"
        "sys.stdin.readline()\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    os.setpgid(0, 0)\n"
        "    os.execv('/bin/sleep', ['sleep', '30'])\n"
        "os.setpgid(pid, pid)\n"
        "os.tcsetpgrp(0, pid)\n"  # the shell foregrounds the job
        "os.waitpid(pid, 0)\n"
        "print('DONE', flush=True)\n"
    )
    try:
        read_until(pty, b"IDLE")
        assert pty.foreground_program() is None
        pty.send_data(b"go\n")
        # the foreground job is now /bin/sleep — resolved by name
        assert wait_for(lambda: pty.foreground_program() == 'sleep')
    finally:
        pty.close()


def test_foreground_program_none_after_child_exits() -> None:
    """After the child exits there is no foreground program — the row
    is dimmed as exited instead."""
    pty = spawn_child("print('BYE', flush=True)\n")
    try:
        read_until(pty, b"BYE")
        assert pty.foreground_program() is None
    finally:
        pty.close()


def test_close_sigkills_a_stubborn_child() -> None:
    # The child ignores EOF/SIGHUP/SIGTERM — only the SIGKILL fallback
    # can stop it (bounded waits, so the test stays fast).
    pty = spawn_child(
        "import signal, time\n"
        "signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "time.sleep(60)\n"
    )
    try:
        read_until(pty, b"", timeout=1.0)
        assert pty.is_running()
    finally:
        pty.close(terminate_timeout=0.2, kill_timeout=0.5)
    assert wait_for(lambda: pty.wait() is not None)
    assert pty.wait() == -signal.SIGKILL


def test_send_data_loops_on_partial_writes(monkeypatch) -> None:
    # A non-blocking master can take short writes — send_data must loop
    # until every byte is written.
    pty = spawn_child("import time\ntime.sleep(60)\n")
    try:
        read_until(pty, b"", timeout=1.0)
        real_write = os.write
        chunks = []

        def capped_write(fd, data):
            n = min(7, len(data))
            chunks.append(n)
            return real_write(fd, data[:n])

        monkeypatch.setattr("pyqtermx.ptyspawn.os.write", capped_write)
        pty.send_data(b"z" * 100)
        assert len(chunks) > 1
        assert sum(chunks) == 100
    finally:
        pty.close()


def test_send_data_retries_on_eagain(monkeypatch) -> None:
    # A full master buffer raises EAGAIN — send_data must retry after
    # the wait, not drop the bytes.
    pty = spawn_child("import time\ntime.sleep(60)\n")
    try:
        read_until(pty, b"", timeout=1.0)
        real_write = os.write
        flaked = {"n": 0}

        def flaky_write(fd, data):
            if flaked["n"] == 0:
                flaked["n"] = 1
                raise BlockingIOError(errno.EAGAIN, "EAGAIN")
            return real_write(fd, data)

        monkeypatch.setattr("pyqtermx.ptyspawn.os.write", flaky_write)
        pty.send_data(b"retry-me")
        assert flaked["n"] == 1
    finally:
        pty.close()


def test_spawn_without_command_uses_shell() -> None:
    if "SHELL" not in os.environ:
        pytest.skip("no $SHELL")
    pty = Pty(None)
    try:
        out = read_until(pty, b"%")  # a prompt appears
        assert out
    finally:
        pty.close()


# -- Control characters → signals (kernel line discipline) ---------------
# The terminal only sends bytes; the pty's line discipline turns them
# into signals (ISIG) or flow control (IXON). These tests prove the
# whole byte→signal path with a real child — the Ctrl+C/SIGINT story
# that htop depends on.

CHILD_SIGNALS = (
    "import signal, sys, time\n"
    "got = {}\n"
    "def handler(sig, frame):\n"
    "    print('GOT:' + got[sig], flush=True)\n"
    "for name in ('SIGINT', 'SIGQUIT', 'SIGTSTP'):\n"
    "    sig = getattr(signal, name)\n"
    "    got[sig] = name\n"
    "    signal.signal(sig, handler)\n"
    "print('READY', flush=True)\n"
    "time.sleep(30)\n"
)


@pytest.mark.parametrize(
    "control,expected",
    [
        (b"\x03", "SIGINT"),  # VINTR ⌃+C
        (b"\x1c", "SIGQUIT"),  # VQUIT ⌃+\
        (b"\x1a", "SIGTSTP"),  # VSUSP ⌃+Z
    ],
)
def test_control_char_delivers_signal(control: bytes, expected: str) -> None:
    pty = spawn_child(CHILD_SIGNALS)
    try:
        read_until(pty, b"READY")
        pty.send_data(control)
        marker = b"GOT:" + expected.encode()
        out = read_until(pty, marker)
        assert marker in out
    finally:
        pty.close()


def test_ctrl_d_is_eof_in_canonical_mode() -> None:
    # VEOF on an empty line: readline returns "" — the shell's EOF.
    pty = spawn_child(
        "import sys\n"
        "print('READY', flush=True)\n"
        "line = sys.stdin.readline()\n"
        "print('EOF' if line == '' else 'DATA:' + line.strip(), flush=True)\n"
    )
    try:
        read_until(pty, b"READY")
        pty.send_data(b"\x04")
        out = read_until(pty, b"EOF")
        assert b"EOF" in out
    finally:
        pty.close()


def test_control_char_is_data_when_isig_off() -> None:
    # Raw mode (ISIG off — htop/ncurses-style): the same byte arrives
    # as *data*; the terminal's job is only to have sent it. This pins
    # the contract: signal vs. data is the app's termios choice.
    pty = spawn_child(
        "import sys, tty\n"
        "tty.setraw(0)\n"
        "print('READY', flush=True)\n"
        "data = sys.stdin.buffer.read(1)\n"
        "print('DATA:%02x' % data[0], flush=True)\n"
    )
    try:
        read_until(pty, b"READY")
        pty.send_data(b"\x03")
        out = read_until(pty, b"DATA:03")
        assert b"DATA:03" in out
    finally:
        pty.close()
