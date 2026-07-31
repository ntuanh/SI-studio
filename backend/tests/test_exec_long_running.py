"""Running a long command from the Control tab: `python3 Server.py`.

Two things had to change for that to work at all. The allow-list refused
`python3` outright, and even past it the exec path killed anything still
running after SSH_COMMAND_TIMEOUT -- which for a measurement run means dying
mid-flight, before it writes its result logs.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from app.ssh import commands as cmds
from app.ssh.commands import CommandRejected
from app.ssh.pool import pool


# --------------------------------------------------------------- the allow-list
@pytest.mark.parametrize(
    "command",
    [
        "python3 server.py",
        "python3 Server.py",
        "python3 src/Server.py",
        "python src/Server.py",
        "python3 /opt/split-inference/src/Server.py --config config.yaml",
        "python3 ./run.py",
        "python3 -u src/Server.py",
    ],
)
def test_running_a_python_script_is_allowed(command):
    """The thing this console is for on the control machine."""
    assert cmds.validate_command(command) == command


@pytest.mark.parametrize(
    "command",
    [
        "python3",                       # bare REPL: no terminal, would hang
        "python",
        "python3 -m http.server",        # only `-m agent…` is admitted
        "python3 -c \"import os;os.system('id')\"",
        "python3 -m SimpleHTTPServer",
    ],
)
def test_the_script_rule_does_not_widen_anything_else(command):
    with pytest.raises(CommandRejected):
        cmds.validate_command(command)


def test_the_script_rule_still_refuses_shell_metacharacters():
    """The guard runs ahead of the allow-list, so a `.py` first argument is
    not a way to smuggle a second command in."""
    with pytest.raises(CommandRejected, match="metacharacter"):
        cmds.validate_command("python3 Server.py; rm -rf /")


# ------------------------------------------------------------------ pty output
@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("plain line\r\n", "plain line"),
        ("\x1b[32mgreen\x1b[0m\r\n", "green"),
        ("10%\r50%\r100% done\r\n", "100% done"),   # progress bar redraw
        ("\x00\x07bell\r\n", "bell"),
    ],
)
def test_pty_lines_are_cleaned_for_the_console(raw, want):
    """A pty returns what a terminal would *draw*; the console renders text."""
    assert cmds.clean_pty_line(raw) == want


# ------------------------------------------------------------------- test doubles
class FakeStdout:
    def __init__(self, lines, done: asyncio.Event) -> None:
        self._lines = list(lines)
        self._done = done

    async def readline(self) -> str:
        if self._lines:
            return self._lines.pop(0)
        await self._done.wait()   # a long run: nothing more until it exits
        return ""


class FakeStdin:
    def __init__(self, process: "FakeProcess") -> None:
        self._process = process

    def write(self, data: str) -> None:
        self._process.written.append(data)
        if cmds.ETX in data:
            self._process.finish(130)   # the terminal driver raised SIGINT


class FakeProcess:
    def __init__(self, lines=(), *, exit_status=0, finishes=True) -> None:
        self._done = asyncio.Event()
        self.stdout = FakeStdout(lines, self._done)
        self.stdin = FakeStdin(self)
        self.written: list[str] = []
        self.signals: list[str] = []
        self.exit_status = exit_status
        if finishes:
            self._done.set()

    def finish(self, exit_status: int = 0) -> None:
        self.exit_status = exit_status
        self._done.set()

    def send_signal(self, sig: str) -> None:
        self.signals.append(sig)

    def terminate(self) -> None:
        self.signals.append("TERM")
        self.finish(143)

    def kill(self) -> None:
        self.signals.append("KILL")
        self.finish(137)

    def close(self) -> None:
        self.finish(137)

    async def wait_closed(self) -> None:
        await self._done.wait()


class FakeConn:
    """One connection handing out one process, recording how it was asked."""

    def __init__(self, process: FakeProcess, *, refuse_pty: bool = False) -> None:
        self.process = process
        self.refuse_pty = refuse_pty
        self.calls: list[dict] = []

    async def create_process(self, cmd: str, **kwargs):
        self.calls.append(kwargs)
        if self.refuse_pty and "term_type" in kwargs:
            raise OSError("pty allocation refused")
        return self.process


@pytest.fixture(autouse=True)
def _no_leaked_jobs():
    """The registry is process-wide, like the pool it borrows from."""
    yield
    cmds.jobs._jobs.clear()


@pytest.fixture()
def seeded(client, auth, ui_export):
    client.post("/seed", json=ui_export, headers=auth)
    return client


def _exec(client, auth, command, **extra):
    body = {"device_ids": ["dA"], "command": command, "detach_after": 0.05}
    body.update(extra)
    return client.post("/control/exec", json=body, headers=auth)


# --------------------------------------------------------------- short commands
def test_a_short_command_still_answers_inline(seeded, auth):
    """Nothing about `ls` changes: it finishes inside the detach window and
    its output comes back in the response, as it always did."""
    conn = FakeConn(FakeProcess(["bin\r\n", "logs\r\n"]))

    with patch.object(pool, "get", return_value=conn):
        body = _exec(seeded, auth, "ls").json()

    assert body["running"] == []
    assert body["ok"] is True
    assert len(body["results"]) == 1
    assert body["results"][0]["stdout"] == "bin\nlogs"
    assert body["results"][0]["exit"] == 0


def test_output_is_asked_for_through_a_pty(seeded, auth):
    """Without one, python block-buffers stdout the moment it sees it is not a
    terminal -- a twenty-minute run would deliver everything at the very end."""
    conn = FakeConn(FakeProcess(["ok\r\n"]))

    with patch.object(pool, "get", return_value=conn):
        _exec(seeded, auth, "ls")

    assert conn.calls[0]["term_type"] == cmds.PTY_TERM_TYPE


def test_a_refused_pty_degrades_instead_of_failing(seeded, auth):
    """Some sshd configs refuse it. The command still has to run."""
    conn = FakeConn(FakeProcess(["ok\r\n"]), refuse_pty=True)

    with patch.object(pool, "get", return_value=conn):
        body = _exec(seeded, auth, "ls").json()

    assert body["ok"] is True
    assert len(conn.calls) == 2                 # pty attempt, then plain
    assert "term_type" not in conn.calls[1]


# ---------------------------------------------------------------- long commands
def test_a_long_run_is_left_running_instead_of_being_killed(seeded, auth):
    """The whole point. `Server.py` runs for the length of the experiment;
    the request answers in milliseconds and the run keeps going."""
    process = FakeProcess(["[FPS] DONE #1\r\n"], finishes=False)
    conn = FakeConn(process)

    with patch.object(pool, "get", return_value=conn):
        body = _exec(seeded, auth, "python3 Server.py").json()

        assert body["results"] == []            # nothing finished...
        assert body["ok"] is True               # ...which is not a failure
        assert len(body["running"]) == 1
        job = body["running"][0]
        assert job["command"] == "python3 Server.py"
        assert job["running"] is True

        listed = seeded.get("/control/jobs", headers=auth).json()["jobs"]
        assert [j["job_id"] for j in listed] == [job["job_id"]]

        # No hard deadline was sent, so none was applied -- the previous
        # behaviour killed this at SSH_COMMAND_TIMEOUT.
        stop = seeded.post(
            "/control/exec/stop", json={"job_id": job["job_id"]}, headers=auth
        ).json()

    assert stop["stopped"][0]["outcome"] == "interrupted"
    assert cmds.ETX in process.written          # a real Ctrl-C, via the pty
    assert seeded.get("/control/jobs", headers=auth).json()["jobs"] == []


def test_stop_escalates_when_ctrl_c_is_ignored(seeded, auth):
    """A run that traps SIGINT must not leave the operator with no way out."""
    process = FakeProcess(finishes=False)
    process.stdin = _DeafStdin(process)
    conn = FakeConn(process)

    with patch.object(pool, "get", return_value=conn):
        body = _exec(seeded, auth, "python3 Server.py").json()
        job_id = body["running"][0]["job_id"]

        with patch.object(cmds, "INTERRUPT_GRACE", 0.05):
            stop = seeded.post(
                "/control/exec/stop", json={"job_id": job_id}, headers=auth
            ).json()

    assert stop["stopped"][0]["outcome"] == "terminated"
    assert "TERM" in process.signals


def test_stop_by_device_covers_whatever_is_running_there(seeded, auth):
    process = FakeProcess(finishes=False)
    conn = FakeConn(process)

    with patch.object(pool, "get", return_value=conn):
        _exec(seeded, auth, "python3 Server.py")
        stop = seeded.post(
            "/control/exec/stop", json={"device_ids": ["dA"]}, headers=auth
        ).json()

    assert len(stop["stopped"]) == 1


def test_stopping_nothing_says_so_rather_than_erroring(seeded, auth):
    r = seeded.post("/control/exec/stop", json={"device_ids": ["dA"]}, headers=auth)
    assert r.status_code == 200
    assert r.json()["stopped"] == []


def test_an_explicit_timeout_is_still_honoured(seeded, auth):
    """`timeout: null` means no limit; a number still means a deadline."""
    process = FakeProcess(finishes=False)
    conn = FakeConn(process)

    with patch.object(pool, "get", return_value=conn):
        body = _exec(
            seeded, auth, "python3 Server.py", timeout=0.05, detach_after=0.4
        ).json()

    assert body["results"], "the timeout should have ended it inside the window"
    assert "timeout after 0.05s" in body["results"][0]["error"]


class _DeafStdin(FakeStdin):
    """A process that ignores Ctrl-C."""

    def write(self, data: str) -> None:
        self._process.written.append(data)
