"""Remote command execution, fan-out, and file push (guide §5).

Security model
--------------
`validate_command()` is the guard for operator-supplied input and is applied at
the router boundary (`POST /control/exec`). Commands the orchestrator builds
itself are trusted and bypass it -- they legitimately need redirects and `&`
(`nohup ... > log 2>&1 &`). Validating at the edge rather than inside
`run_command` keeps that distinction explicit instead of relying on a flag every
internal caller could forget.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shlex
import time
import uuid
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import asyncssh

from ..config import settings
from ..models import Device
from ..services.metrics_bus import bus
from .pool import SSHError, SSHPool

log = logging.getLogger(__name__)


# --------------------------------------------------------------------- results
@dataclass
class CmdResult:
    device_id: str
    device_name: str = ""
    host: str = ""
    command: str = ""
    stdout: str = ""
    stderr: str = ""
    exit: int | None = None
    error: str = ""
    duration_ms: float = 0.0

    @property
    def ok(self) -> bool:
        return self.error == "" and self.exit == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "device_name": self.device_name,
            "host": self.host,
            "command": self.command,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit": self.exit,
            "error": self.error,
            "duration_ms": round(self.duration_ms, 1),
            "ok": self.ok,
        }


@dataclass
class ScpResult:
    device_id: str
    device_name: str = ""
    host: str = ""
    remote_path: str = ""
    bytes_sent: int = 0
    error: str = ""
    duration_ms: float = 0.0
    checksum_ok: bool | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error == ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "device_name": self.device_name,
            "host": self.host,
            "remote_path": self.remote_path,
            "bytes_sent": self.bytes_sent,
            "error": self.error,
            "duration_ms": round(self.duration_ms, 1),
            "checksum_ok": self.checksum_ok,
            "ok": self.ok,
            **self.extra,
        }


# ------------------------------------------------------------------- allow-list
#: Command prefixes the Control tab may run. Covers all 14 UI preset buttons
#: plus the read-only probes and agent lifecycle commands.
ALLOWED_PREFIXES: tuple[str, ...] = (
    # --- UI presets: inspection ---
    "nvidia-smi",              # incl. --query-gpu=temperature.gpu (GPU temp)
    "uptime",
    "nproc",
    "df",                      # disk
    "free",                    # memory
    "iperf3",                  # iperf3 -c $BROKER_IP -t 5
    "ping",                    # ping -c 3 $BROKER_IP
    # --- UI presets: agent lifecycle ---
    "systemctl status inference-agent",
    "systemctl restart inference-agent",   # destructive -> needs confirm
    "systemctl stop inference-agent",      # destructive -> needs confirm
    "systemctl start inference-agent",
    "sudo systemctl status inference-agent",
    "sudo systemctl restart inference-agent",
    "sudo systemctl stop inference-agent",
    "sudo systemctl start inference-agent",
    "journalctl -u inference-agent",       # tail logs (-n 50 --no-pager)
    "sudo journalctl -u inference-agent",
    # --- UI preset: reboot (destructive -> needs confirm) ---
    "sudo reboot",
    "reboot",
    # --- additional read-only probes ---
    "hostname",
    "uname",
    "whoami",
    "lscpu",
    "ls",
    "sha256sum",
    "tail",
    # --- read-only inspection ---
    # Looking at files and processes is the bread and butter of operating a
    # machine, and refusing it just pushes people to save the same commands as
    # presets, which allows them anyway with an extra step. None of these
    # change anything; anything that does still has to be added deliberately.
    "cat",
    "head",
    "grep",
    "wc",
    "pwd",
    "du",
    "stat",
    "file",
    "env",
    "id",
    "date",
    "ps",
    "md5sum",
    "which",
    "echo",
    "lsblk",
    "ip",
    "ss",
    "top -b",
    "docker ps",
    "docker images",
    "git status",
    "git log",
    "git diff",
    "git branch",
    "git remote",
)

#: `python -c` / `python3 -c`, admitted only for the read-only introspection
#: preset -- see `_python_inline_is_safe`.
PYTHON_INLINE_PREFIXES: tuple[str, ...] = (
    "python -c",
    "python3 -c",
)

#: `python3 Server.py`, `python3 src/Server.py --config x.yaml`, ... -- running
#: the project's own scripts.
#:
#: This is what the console exists for on the control machine: starting
#: `Server.py` from here is the same act as starting it over SSH by hand, which
#: is what the operator does today with a terminal open next to this page.
#: Admitting it is therefore no wider than the access the SSH credential the
#: operator already configured hands out.
#:
#: Narrower than a bare `python3` prefix on purpose. That would re-admit the
#: REPL (nothing to type into, so it would hang -- see INTERACTIVE_PROGRAMS)
#: and `python3 -c <anything>`, which keeps its own far stricter rule above.
#: The first non-flag argument has to be a `.py` file, and the only flags
#: allowed before it are the argument-less ones, so `-c` and `-m` cannot sneak
#: through.
PYTHON_SCRIPT_RE = re.compile(r"^(?:python|python3)(?:\s+-[uBOEsSI]+)*\s+[^\s-]\S*\.py(?:\s|$)")

#: Prefixes that may also be continued by a dot, so `python3 -m agent` matches
#: `python3 -m agent.edge_agent --help`. Kept separate from ALLOWED_PREFIXES
#: because a general dot boundary would also admit things like `df.sh`.
ALLOWED_MODULE_PREFIXES: tuple[str, ...] = (
    "python -m agent",
    "python3 -m agent",
)

#: Shell metacharacters that turn one command into several, when unquoted.
_UNSAFE_CHARS = set(";&|<>`\n\r\\")

#: Destructive presets. Allowed, but only with an explicit `confirm=true`
#: on /control/exec, and always written to the audit log.
DESTRUCTIVE_PREFIXES: tuple[str, ...] = (
    "sudo reboot",
    "reboot",
    "sudo shutdown",
    "shutdown",
    "systemctl stop inference-agent",
    "systemctl restart inference-agent",
    "sudo systemctl stop inference-agent",
    "sudo systemctl restart inference-agent",
)

#: `python -c` is arbitrary code execution, so only read-only introspection is
#: accepted: the script may reference these modules and nothing else.
_PY_SAFE_MODULES = frozenset({"torch", "numpy", "np", "pika", "platform", "sys_version"})
_PY_SAFE_SCRIPT = re.compile(r"^[\w\s;.,()\[\]'\"=+*/-]*$")
_PY_FORBIDDEN = re.compile(
    r"\b(os|sys|subprocess|shutil|socket|pty|pathlib|builtins|importlib|ctypes|"
    r"open|eval|exec|compile|input|__import__|globals|locals|getattr|setattr)\b"
)

#: Token the UI's iperf3/ping presets use for the configured broker address.
BROKER_IP_TOKEN = "$BROKER_IP"

#: Addresses that mean "this machine" -- useless once the command is running on
#: a device rather than here.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", "127.0.1.1"})


class CommandRejected(ValueError):
    """Operator-supplied command failed the allow-list / metacharacter guard."""


class ConfirmationRequired(CommandRejected):
    """A destructive command was submitted without `confirm=true`."""


def substitute_broker_ip(cmd: str, broker_host: str | None) -> str:
    """Replace `$BROKER_IP` with the configured broker address.

    Idempotent, so it is safe to call both at the router boundary (before
    validation, which must see the final string) and inside `fan_out`.
    """
    command = cmd or ""
    if BROKER_IP_TOKEN not in command:
        return command
    host = (broker_host or "").strip()
    if not host:
        raise CommandRejected(
            f"{BROKER_IP_TOKEN} is used but no broker host is configured; "
            "POST /server/config first"
        )
    if host.lower() in LOOPBACK_HOSTS:
        # These commands run *on the devices*: `iperf3 -c localhost` there
        # measures that device's own loopback and reports tens of Gbit/s,
        # which then flows into its bandwidth spec via /devices/{id}/probe.
        # Failing is far better than a plausible, meaningless number.
        raise CommandRejected(
            f"{BROKER_IP_TOKEN} resolves to {host!r}, which means 'this machine' "
            "on the device, not the broker. Set an AMQP host the devices can "
            "actually route to (the broker card's AMQP host field)."
        )
    return command.replace(BROKER_IP_TOKEN, host)


#: Programs that need a terminal they will never get.
#:
#: Commands run over an SSH *exec* channel: no PTY, no keyboard, and the output
#: is captured rather than drawn. A full-screen editor or pager has nothing to
#: draw on and nothing to read from, so it can only waste the command timeout.
#: Refusing up front turns a two-minute silence into an immediate explanation.
INTERACTIVE_PROGRAMS: dict[str, str] = {
    "vim": "edit the file locally and push it with SCP, or read it with `cat`",
    "vi": "edit the file locally and push it with SCP, or read it with `cat`",
    "nvim": "edit the file locally and push it with SCP, or read it with `cat`",
    "vimdiff": "edit the file locally and push it with SCP, or read it with `cat`",
    "view": "read it with `cat`",
    "nano": "edit the file locally and push it with SCP, or read it with `cat`",
    "emacs": "edit the file locally and push it with SCP, or read it with `cat`",
    "pico": "edit the file locally and push it with SCP, or read it with `cat`",
    "less": "use `cat`, or `tail -n 100`",
    "more": "use `cat`, or `tail -n 100`",
    "man": "use `--help`",
    "top": "use `top -b -n 1`",
    "htop": "use `top -b -n 1`",
    "watch": "run the command once; there is no screen to refresh",
    "ssh": "add that machine as its own target instead of hopping from here",
    "sudo": "prefix-only `sudo` needs a password prompt; use a specific sudo command",
    "su": "connect as that user instead",
    "mysql": "pass the query with -e",
    "psql": "pass the query with -c",
    "screen": "there is no terminal to attach to",
    "tmux": "there is no terminal to attach to",
    # Bare interpreters are REPLs; `python x.py` and `python -c …` are not, and
    # `_interactive_program` lets those through.
    "python": "pass a script or `-c`",
    "python3": "pass a script or `-c`",
}

#: This table is a courtesy, not a guarantee -- it cannot name every program
#: that wants a terminal. The real backstop is that `run_command` closes stdin,
#: so anything expecting input gets EOF and exits instead of hanging.


def _interactive_program(command: str) -> tuple[str, str] | None:
    """The interactive program this command would launch, if any."""
    parts = command.split()
    if not parts:
        return None
    head = PurePosixPath(parts[0]).name  # /usr/bin/vim -> vim

    # `sudo vim x` is as interactive as `vim x`; look past the wrapper. Bare
    # `sudo` on its own is caught by the table.
    if head == "sudo" and len(parts) > 1:
        head = PurePosixPath(parts[1]).name

    hint = INTERACTIVE_PROGRAMS.get(head)
    if hint is None:
        return None

    # A bare interpreter is a REPL; with a script or -c it is a batch run.
    if head in ("python", "python3") and len(parts) > 1:
        return None
    return head, hint


#: Paths that stay off-limits however the command is spelled.
#:
#: Read-only commands are broadly allowed because inspecting a machine is the
#: ordinary work this console exists for. That reasoning does not extend to
#: credentials: whoever holds the API token is not necessarily whoever knows
#: the SSH password, so `cat` must not become a way to lift the latter -- or
#: the private keys that reach every *other* machine in the inventory.
SENSITIVE_PATH_RE = re.compile(
    r"""(?xi)
    /etc/(shadow|gshadow|sudoers)\b
  | (^|[\s/'"])\.?ssh/(id_[a-z0-9]+|authorized_keys|known_hosts)\b
  | \bid_(rsa|dsa|ecdsa|ed25519)\b
  | \.(pem|pfx|p12|jks)\b
  | \.aws/credentials\b
  | \.kube/config\b
  | \bcredentials\.json\b
    """
)


def reject_if_sensitive(command: str) -> None:
    match = SENSITIVE_PATH_RE.search(command)
    if match is None:
        return
    raise CommandRejected(
        f"refusing to touch {match.group(0).strip()!r}: credentials and private "
        f"keys are off-limits to the control API, which is not the same trust "
        f"level as an SSH login. Set ALLOW_UNSAFE_COMMANDS=true if you really "
        f"mean it."
    )


def reject_if_interactive(command: str) -> None:
    program = _interactive_program(command)
    if program is None:
        return
    head, hint = program
    raise CommandRejected(
        f"{head!r} needs an interactive terminal, which this transport does not "
        f"provide (commands run without a TTY and their output is captured). "
        f"Instead: {hint}."
    )


def with_working_directory(command: str, cwd: str | None) -> str:
    """Prefix `cd <cwd> &&` so the command runs somewhere other than $HOME.

    Every command gets its own SSH exec channel, i.e. its own shell, so a bare
    `cd foo` changes a directory that ceases to exist a moment later -- which
    is why "run `cd`, then run `ls`" cannot work however the allow-list is
    configured. The directory has to travel *with* each command instead.

    Built here rather than typed by the operator: `&&` is exactly what the
    metacharacter guard rejects in operator input, and `shlex.quote` means a
    path containing `;` or `$(` cannot smuggle a second command past it.
    """
    directory = (cwd or "").strip()
    if not directory:
        return command
    if "\n" in directory or "\r" in directory:
        raise CommandRejected("working directory must be a single line")
    return f"cd {shlex.quote(directory)} && {command}"


def _unsafe_metacharacter(command: str) -> str | None:
    """Find a shell metacharacter that is *not* inside quotes.

    A plain regex over the whole string rejects legitimate quoted arguments such
    as ``python -c "import torch;print(...)"``. Shell quoting rules apply:
      * inside single quotes everything is literal;
      * inside double quotes ``$`` and a backtick still expand, but ``;``,
        ``&``, ``|``, ``<`` and ``>`` do not.
    """
    quote: str | None = None
    i = 0
    while i < len(command):
        ch = command[i]

        if quote == "'":
            if ch == "'":
                quote = None
            i += 1
            continue

        if quote == '"':
            if ch == '"':
                quote = None
            elif ch == "`":
                return "`"
            elif ch == "$" and i + 1 < len(command) and command[i + 1] in "({":
                return command[i : i + 2]
            i += 1
            continue

        if ch in "'\"":
            quote = ch
        elif ch == "$" and i + 1 < len(command) and command[i + 1] in "({":
            return command[i : i + 2]
        elif ch in _UNSAFE_CHARS:
            return ch
        i += 1

    if quote is not None:
        return "unbalanced quote"
    return None


def _python_inline_is_safe(command: str) -> bool:
    """Allow `python -c` only for read-only version/capability introspection."""
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    if len(parts) != 3 or parts[1] != "-c":
        return False
    script = parts[2]
    if not _PY_SAFE_SCRIPT.match(script) or _PY_FORBIDDEN.search(script):
        return False
    imported = set(re.findall(r"\bimport\s+([\w.]+)", script))
    return bool(imported) and all(m.split(".")[0] in _PY_SAFE_MODULES for m in imported)


def is_destructive(cmd: str) -> bool:
    command = (cmd or "").strip()
    return any(
        command == p or command.startswith(p + " ") for p in DESTRUCTIVE_PREFIXES
    )


def validate_command(
    cmd: str,
    *,
    allow_unsafe: bool | None = None,
    confirm: bool = False,
    saved: Collection[str] | None = None,
) -> str:
    """Return the normalized command, or raise `CommandRejected`.

    `cmd` must already have `$BROKER_IP` substituted -- the allow-list has to
    see the final string that will reach the shell.

    Set ALLOW_UNSAFE_COMMANDS=true (server-side, §5) to skip the metacharacter
    and allow-list checks. The destructive-command confirmation still applies:
    it guards against accidents, not against a hostile operator.
    """
    command = (cmd or "").strip()
    if not command:
        raise CommandRejected("command is empty")

    if is_destructive(command) and not confirm:
        raise ConfirmationRequired(
            f"{command!r} is destructive; resend with confirm=true to run it"
        )

    unsafe_ok = settings.allow_unsafe_commands if allow_unsafe is None else allow_unsafe
    if unsafe_ok:
        return command

    # Ahead of the allow-list and of the saved-preset escape hatch: broadly
    # permitting read-only commands must not quietly become a way to read
    # credentials, and saving one as a preset must not either.
    reject_if_sensitive(command)

    if (found := _unsafe_metacharacter(command)) is not None:
        raise CommandRejected(
            f"unquoted shell metacharacter {found!r} is not allowed; "
            "set ALLOW_UNSAFE_COMMANDS=true server-side to permit it"
        )

    allowed = (
        any(command == p or command.startswith(p + " ") for p in ALLOWED_PREFIXES)
        or any(
            command == p or command.startswith(p + " ") or command.startswith(p + ".")
            for p in ALLOWED_MODULE_PREFIXES
        )
        or (
            any(command.startswith(p + " ") for p in PYTHON_INLINE_PREFIXES)
            and _python_inline_is_safe(command)
        )
        # `python3 Server.py …` -- see PYTHON_SCRIPT_RE.
        or PYTHON_SCRIPT_RE.match(command) is not None
        # The operator's own saved presets. Writing one down is an explicit
        # statement of intent, which is exactly the thing the allow-list is
        # trying to establish for a command typed ad hoc.
        or command in (saved or ())
    )
    if not allowed:
        head = command.split()[0]
        raise CommandRejected(
            f"command {head!r} is not on the allow-list "
            f"({', '.join(ALLOWED_PREFIXES[:6])}, ...). "
            f"Add it under 'edit' next to the command presets to run it."
        )

    return command


# ------------------------------------------------------------------ single host
async def run_command(
    conn: asyncssh.SSHClientConnection,
    cmd: str,
    *,
    timeout: float | None = None,
) -> CmdResult:
    """Run `cmd` on an open connection. Never raises on a non-zero exit."""
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    result = CmdResult(device_id="", command=cmd)
    try:
        r = await asyncio.wait_for(
            # `input=""` closes stdin immediately. Without it, anything that
            # reads stdin -- an editor, a `read`, an apt prompt -- blocks until
            # the timeout with nothing on screen. An EOF makes it exit at once
            # and report something the operator can act on.
            conn.run(cmd, check=False, input=""),
            timeout=timeout if timeout is not None else settings.ssh_command_timeout,
        )
        result.stdout = _text(r.stdout)
        result.stderr = _text(r.stderr)
        result.exit = r.exit_status
    except asyncio.TimeoutError:
        result.error = f"timeout after {timeout or settings.ssh_command_timeout}s"
    except (OSError, asyncssh.Error) as exc:
        result.error = str(exc)
    result.duration_ms = (loop.time() - t0) * 1000.0
    return result


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


# ------------------------------------------- long-running commands, live output
#: Terminal requested for the pty. Wide enough that a framed summary block or a
#: progress line is not wrapped into unreadable fragments.
PTY_TERM_TYPE = "xterm"
PTY_TERM_SIZE = (200, 50)

#: Ctrl-C, written into the pty.
#:
#: `send_signal('INT')` is the obvious route and is still attempted as a
#: fallback, but OpenSSH's sshd does not implement the `signal` channel request
#: -- it accepts and drops it. Writing the byte into a pty makes the *remote
#: terminal driver* raise SIGINT against the foreground process group, which is
#: what pressing Ctrl-C in a terminal actually does.
ETX = "\x03"

#: How long each escalation step waits before the next one.
INTERRUPT_GRACE = 3.0

#: A pty hands back what a terminal would *draw*; the console renders text.
#: Colour and cursor-movement sequences would otherwise show up as literal
#: `[0;32m` noise, and a raw control byte would corrupt the JSON frame.
_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"          # CSI: colours, cursor moves
    r"|\x1b[@-Z\\-_]"                     # two-character escapes
    r"|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]"  # stray control bytes
)


def clean_pty_line(raw: str) -> str:
    """One line off the pty, as the console should show it."""
    text = raw.replace("\r\n", "\n").rstrip("\n")
    if "\r" in text:
        # A progress bar redraws by returning to column 0 and overwriting. Only
        # the last thing written to the line is what a terminal would show.
        text = text.rsplit("\r", 1)[-1]
    return _ANSI_RE.sub("", text)


@dataclass
class ExecJob:
    """A command still running after `/control/exec` has already answered.

    Tracked because otherwise a long run would be started and then be
    unreachable: the request that launched it is long gone and there is no
    shell session left to press Ctrl-C in.
    """

    id: str
    device_id: str
    device_name: str
    host: str
    command: str
    started_at: float
    pty: bool = True
    process: Any = None                     # asyncssh.SSHClientProcess
    task: asyncio.Task[CmdResult] | None = None
    result: CmdResult | None = None

    @property
    def running(self) -> bool:
        return self.result is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.id,
            "device_id": self.device_id,
            "device_name": self.device_name,
            "host": self.host,
            "command": self.command,
            "running": self.running,
            "pty": self.pty,
            "started_at": self.started_at,
            "elapsed_s": round(time.time() - self.started_at, 1),
        }


class JobRegistry:
    """Every `/control/exec` command still running, keyed by job id."""

    def __init__(self) -> None:
        self._jobs: dict[str, ExecJob] = {}

    def add(self, job: ExecJob) -> None:
        self._jobs[job.id] = job

    def discard(self, job_id: str) -> None:
        self._jobs.pop(job_id, None)

    def get(self, job_id: str) -> ExecJob | None:
        return self._jobs.get(job_id)

    def running(self, device_ids: Collection[str] | None = None) -> list[ExecJob]:
        wanted = set(device_ids) if device_ids else None
        return [
            j
            for j in self._jobs.values()
            if j.running and (wanted is None or j.device_id in wanted)
        ]

    async def interrupt(self, job: ExecJob, *, grace: float | None = None) -> str:
        """Ctrl-C, then SIGTERM, then drop the channel. Returns what worked.

        Escalating rather than killing outright: `Server.py` writes its result
        logs on the way out, and a SIGKILL would cost the run's numbers. The
        polite signal is given a real chance first.
        """
        grace = INTERRUPT_GRACE if grace is None else grace
        process = job.process
        if process is None or not job.running:
            return "already finished"

        # 1. What the operator would press.
        if job.pty:
            with contextlib.suppress(OSError, BrokenPipeError, asyncssh.Error):
                process.stdin.write(ETX)
        with contextlib.suppress(OSError, asyncssh.Error):
            process.send_signal("INT")
        if await self._settled(job, grace):
            return "interrupted"

        # 2. It ignored Ctrl-C, or sshd dropped the signal and there was no pty.
        with contextlib.suppress(OSError, asyncssh.Error):
            process.terminate()
        if await self._settled(job, grace):
            return "terminated"

        # 3. Give up on politeness. Closing the channel also hangs up the pty,
        #    which is a SIGHUP for anything still attached to it.
        with contextlib.suppress(OSError, asyncssh.Error):
            process.kill()
        with contextlib.suppress(OSError, asyncssh.Error):
            process.close()
        await self._settled(job, grace)
        return "killed"

    @staticmethod
    async def _settled(job: ExecJob, grace: float) -> bool:
        """Wait up to `grace` for the pump to notice the process exited."""
        if job.task is None or job.task.done():
            return True
        done, _ = await asyncio.wait({job.task}, timeout=grace)
        return bool(done)


#: Process-wide, like the SSH pool it borrows connections from.
jobs = JobRegistry()


async def _open_process(
    conn: asyncssh.SSHClientConnection, cmd: str
) -> tuple[Any, bool]:
    """Start `cmd` under a pty, falling back to a plain exec channel.

    The pty is the point. Without one, python block-buffers stdout as soon as
    it sees it is not a terminal, so a twenty-minute run would deliver its
    output in 8 KB slabs -- or, far more often, all at once at the very end.
    With one, output is line-buffered and shows up as it is printed, and Ctrl-C
    becomes deliverable.

    Some appliances and locked-down sshd configs refuse the pty request. That
    is worth degrading for rather than failing on: the command still runs, and
    the caller is told which mode it got so the console can say so.
    """
    try:
        process = await conn.create_process(
            cmd,
            term_type=PTY_TERM_TYPE,
            term_size=PTY_TERM_SIZE,
            encoding="utf-8",
            errors="replace",
        )
        return process, True
    except (OSError, asyncssh.Error) as exc:
        log.info("pty refused (%s); falling back to a plain exec channel", exc)

    process = await conn.create_process(
        cmd,
        stderr=asyncssh.STDOUT,  # one ordered stream, as the pty would have given
        encoding="utf-8",
        errors="replace",
    )
    return process, False


async def _pump(
    job: ExecJob,
    *,
    timeout: float | None,
    stream: bool,
    on_line: Callable[[str], None] | None = None,
) -> CmdResult:
    """Read the job's output to EOF, publishing each line as it arrives.

    This is the difference between watching a run and staring at a spinner:
    lines reach the browser while the command is still going, instead of being
    accumulated and posted after it finishes.

    `on_line` is for a caller that needs the same lines *while* they arrive
    rather than in `result.stdout` at the end -- the project queue reads the
    server's batch counter out of them (`services/project_queue.py`). It is
    called inside the read loop, so it must not block and must not raise; a
    misbehaving hook is caught here rather than being allowed to end a run it
    was only supposed to watch.
    """
    process = job.process
    result = CmdResult(
        device_id=job.device_id,
        device_name=job.device_name,
        host=job.host,
        command=job.command,
    )
    started = time.monotonic()
    collected: list[str] = []

    async def read_all() -> None:
        while True:
            raw = await process.stdout.readline()
            if not raw:
                break
            line = clean_pty_line(raw)
            collected.append(line)
            if on_line is not None:
                try:
                    on_line(line)
                except Exception:  # noqa: BLE001 - a watcher must not kill the run
                    log.exception("output hook failed for job %s", job.id)
            if stream:
                bus.exec_line(job.device_id, f"│  {line}", "stdout")

    try:
        if timeout is None:
            await read_all()
        else:
            await asyncio.wait_for(read_all(), timeout=timeout)
        await process.wait_closed()
        result.exit = process.exit_status
    except asyncio.TimeoutError:
        result.error = f"timeout after {timeout}s"
        with contextlib.suppress(OSError, asyncssh.Error):
            process.terminate()
        with contextlib.suppress(OSError, asyncssh.Error):
            process.close()
    except asyncio.CancelledError:
        # The service is shutting down. Leave the remote alone and re-raise:
        # swallowing this would strand the task.
        raise
    except (OSError, asyncssh.Error) as exc:
        result.error = str(exc)

    result.stdout = "\n".join(collected)
    result.duration_ms = (time.monotonic() - started) * 1000.0

    # Published before the registry drops the job so a client watching the
    # console and a client polling /control/jobs cannot disagree.
    if stream:
        if result.error:
            bus.exec_line(job.device_id, f"└─ error: {result.error}", "stderr")
        else:
            bus.exec_line(
                job.device_id,
                f"└─ done (exit {result.exit})",
                "stdout" if result.ok else "stderr",
            )

    job.result = result
    jobs.discard(job.id)
    return result


async def start_job(
    pool: SSHPool,
    device: Device,
    cmd: str,
    *,
    timeout: float | None = None,
    stream: bool = True,
    on_line: Callable[[str], None] | None = None,
) -> ExecJob:
    """Open a channel, start `cmd`, and begin pumping its output.

    Returns as soon as the command is *running* -- how long to wait for it to
    finish is the caller's decision (`wait_or_detach`). A connection or channel
    failure comes back as a finished job carrying the error, so the caller has
    one shape to handle rather than two.
    """
    host = f"{device.username}@{device.host}"
    job = ExecJob(
        id=uuid.uuid4().hex[:8],
        device_id=device.id,
        device_name=device.name,
        host=host,
        command=cmd,
        started_at=time.time(),
    )

    try:
        conn = await pool.get(device)
        job.process, job.pty = await _open_process(conn, cmd)
    except (SSHError, OSError, asyncssh.Error) as exc:
        job.result = CmdResult(
            device_id=device.id, device_name=device.name, host=host,
            command=cmd, error=str(exc),
        )
        if stream:
            bus.exec_line(device.id, f"✗ {device.name}: {exc}", "stderr")
        return job

    if stream:
        note = "" if job.pty else "  (no pty: output may arrive in blocks)"
        bus.exec_line(device.id, f"┌─ {device.name} ({host}){note}", "meta")

    # Registered before the pump starts: `_pump` discards the job on exit, and
    # a fast command must not manage to do that before it was ever added.
    jobs.add(job)
    job.task = asyncio.create_task(
        _pump(job, timeout=timeout, stream=stream, on_line=on_line), name=f"exec-{job.id}"
    )
    return job


async def wait_or_detach(job: ExecJob, settle: float) -> bool:
    """Wait `settle` seconds for the job to finish. True if it did.

    A short command (`ls`, `nvidia-smi`) finishes inside the window and is
    reported inline, exactly as before. A long one (`python3 Server.py`) does
    not, and is left running: the request returns, and its output keeps
    arriving on the stream. Blocking until it finished instead would tie a
    twenty-minute run to one HTTP request that no proxy or browser will hold
    open that long.
    """
    if job.task is None:          # failed before it started
        return True
    done, _ = await asyncio.wait({job.task}, timeout=max(0.0, settle))
    return bool(done)


async def run_on_device(
    pool: SSHPool,
    device: Device,
    cmd: str,
    *,
    timeout: float | None = None,
    stream: bool = True,
) -> CmdResult:
    """Connect (or reuse), run, and stream each output line to the UI console."""
    host = f"{device.username}@{device.host}"
    try:
        conn = await pool.get(device)
    except SSHError as exc:
        res = CmdResult(
            device_id=device.id, device_name=device.name, host=host,
            command=cmd, error=str(exc),
        )
        if stream:
            bus.exec_line(device.id, f"✗ {device.name}: {exc}", "stderr")
        return res

    res = await run_command(conn, cmd, timeout=timeout)
    res.device_id, res.device_name, res.host = device.id, device.name, host

    if stream:
        bus.exec_line(device.id, f"┌─ {device.name} ({host})", "meta")
        for line in (res.stdout or "").splitlines():
            bus.exec_line(device.id, f"│  {line}", "stdout")
        for line in (res.stderr or "").splitlines():
            bus.exec_line(device.id, f"│  {line}", "stderr")
        if res.error:
            bus.exec_line(device.id, f"└─ error: {res.error}", "stderr")
        else:
            bus.exec_line(device.id, f"└─ done (exit {res.exit})", "stdout" if res.ok else "stderr")

    return res


# --------------------------------------------------------------------- fan-out
async def fan_out(
    pool: SSHPool,
    devices: list[Device],
    cmd: str,
    *,
    timeout: float | None = None,
    stream: bool = True,
    concurrency: int | None = None,
) -> list[CmdResult]:
    """Run one command across many devices, bounded by a Semaphore (§5).

    `$BROKER_IP` is resolved here against the configured server host. The call
    is idempotent, so the router substituting first (which it must, so the
    allow-list sees the final string) does not double-substitute.
    """
    from ..services.server_state import server_state

    cmd = substitute_broker_ip(cmd, server_state.host)
    sem = asyncio.Semaphore(concurrency or settings.fanout_concurrency)

    async def one(d: Device) -> CmdResult:
        async with sem:
            return await run_on_device(pool, d, cmd, timeout=timeout, stream=stream)

    return list(await asyncio.gather(*(one(d) for d in devices)))


async def fan_out_jobs(
    pool: SSHPool,
    devices: list[Device],
    cmd: str,
    *,
    settle: float,
    timeout: float | None = None,
    stream: bool = True,
    concurrency: int | None = None,
) -> tuple[list[CmdResult], list[ExecJob]]:
    """Like `fan_out`, but streams live and does not have to wait for the end.

    Returns `(finished, still_running)`. Anything still running after `settle`
    keeps going in the background with its output still flowing to the console.

    The semaphore bounds *starting* the commands, not their lifetime -- a
    long-running job holding a fan-out slot for twenty minutes would block
    every later command on an unrelated machine.
    """
    from ..services.server_state import server_state

    cmd = substitute_broker_ip(cmd, server_state.host)
    sem = asyncio.Semaphore(concurrency or settings.fanout_concurrency)

    async def one(d: Device) -> ExecJob:
        async with sem:
            return await start_job(pool, d, cmd, timeout=timeout, stream=stream)

    started = list(await asyncio.gather(*(one(d) for d in devices)))
    await asyncio.gather(*(wait_or_detach(j, settle) for j in started))

    finished = [j.result for j in started if j.result is not None]
    return finished, [j for j in started if j.running]


# ------------------------------------------------------------------------- scp
async def scp_put(
    conn: asyncssh.SSHClientConnection,
    local_path: str | Path,
    remote_path: str,
) -> int:
    """Push one file, creating the remote parent directory first.

    Returns the number of bytes sent.
    """
    local = Path(local_path)
    parent = str(PurePosixPath(remote_path).parent)
    if parent not in ("", "/", "."):
        await conn.run(f"mkdir -p {shlex.quote(parent)}", check=False)
    await asyncssh.scp(str(local), (conn, remote_path))
    return local.stat().st_size


async def sftp_list(
    conn: asyncssh.SSHClientConnection, remote_path: str
) -> list[dict[str, Any]]:
    """Directory listing over SFTP.

    Not `ls` over an exec channel: parsing `ls` output is guesswork the moment
    a filename contains a space, and SFTP hands back sizes and types as data.
    """
    path = (remote_path or ".").strip() or "."
    async with conn.start_sftp_client() as sftp:
        if not await sftp.isdir(path):
            attrs = await sftp.stat(path)
            return [{
                "name": PurePosixPath(path).name or path,
                "path": path,
                "dir": False,
                "size": int(attrs.size or 0),
                "mtime": int(attrs.mtime or 0),
            }]

        entries = []
        for name in await sftp.listdir(path):
            if name in (".", ".."):
                continue
            full = str(PurePosixPath(path) / name)
            try:
                attrs = await sftp.stat(full)
                is_dir = await sftp.isdir(full)
            except (OSError, asyncssh.Error):
                # A broken symlink or a file we cannot stat should not sink
                # the whole listing.
                entries.append({"name": name, "path": full, "dir": False,
                                "size": 0, "mtime": 0, "error": True})
                continue
            entries.append({
                "name": name,
                "path": full,
                "dir": is_dir,
                "size": 0 if is_dir else int(attrs.size or 0),
                "mtime": int(attrs.mtime or 0),
            })

    # Directories first, then by name -- the order you would expect to read.
    entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
    return entries


async def sftp_get(
    conn: asyncssh.SSHClientConnection, remote_path: str, local_path: Path
) -> int:
    """Pull one file down. Returns the number of bytes received."""
    async with conn.start_sftp_client() as sftp:
        attrs = await sftp.stat(remote_path)
        if await sftp.isdir(remote_path):
            raise IsADirectoryError(f"{remote_path} is a directory")
        await sftp.get(remote_path, str(local_path))
    return int(attrs.size or local_path.stat().st_size)


#: Result-directory pulls walk whatever the experiment left behind, so the
#: limits are the guard rail rather than the operator's judgement.
DIR_MAX_FILES = 400
DIR_MAX_BYTES = 256 * 1024**2
DIR_MAX_DEPTH = 8


async def sftp_get_dir(
    conn: asyncssh.SSHClientConnection,
    remote_path: str,
    local_root: Path,
    *,
    suffixes: Collection[str] | None = None,
    max_files: int = DIR_MAX_FILES,
    max_bytes: int = DIR_MAX_BYTES,
    max_depth: int = DIR_MAX_DEPTH,
) -> dict[str, Any]:
    """Pull a whole result directory down, filtered and bounded.

    Walked over SFTP rather than tarred over an exec channel: the pull has to
    work on a device where the control API is not allowed to run arbitrary
    shell, and `tar` on an unknown directory is exactly the unbounded operation
    the limits below exist to prevent.

    `suffixes` keeps the transfer to the files a report can read -- a results
    directory frequently sits next to a 300 MB checkpoint that has nothing to
    say. Skipped and truncated are reported, never silent: a chart drawn from
    half a directory has to say so.
    """
    root = (remote_path or ".").rstrip("/") or "/"
    wanted = {s.lower() for s in suffixes} if suffixes else None
    pulled: list[dict[str, Any]] = []
    skipped: list[str] = []
    total = 0
    truncated = ""

    async with conn.start_sftp_client() as sftp:
        if not await sftp.isdir(root):
            raise NotADirectoryError(f"{root} is not a directory")

        seen: set[str] = set()
        queue: list[tuple[str, int]] = [(root, 0)]
        while queue:
            if truncated:
                break
            current, depth = queue.pop(0)
            try:
                real = str(await sftp.realpath(current))
            except (OSError, asyncssh.Error):
                real = current
            if real in seen:  # a symlink pointing back up the tree
                continue
            seen.add(real)

            try:
                names = await sftp.listdir(current)
            except (OSError, asyncssh.Error) as exc:
                skipped.append(f"{current}: {exc}")
                continue

            for name in sorted(names):
                if name in (".", "..") or name.startswith("."):
                    continue
                full = f"{current.rstrip('/')}/{name}"
                rel = full[len(root):].lstrip("/")
                try:
                    if await sftp.isdir(full):
                        if depth + 1 <= max_depth:
                            queue.append((full, depth + 1))
                        else:
                            skipped.append(f"{rel}/ (deeper than {max_depth} levels)")
                        continue
                    attrs = await sftp.stat(full)
                except (OSError, asyncssh.Error) as exc:
                    skipped.append(f"{rel}: {exc}")
                    continue

                size = int(attrs.size or 0)
                suffix = PurePosixPath(name).suffix.lower()
                if wanted is not None and suffix not in wanted:
                    skipped.append(rel)
                    continue
                if len(pulled) >= max_files:
                    truncated = f"stopped after {max_files} files"
                    break
                if total + size > max_bytes:
                    truncated = f"stopped at {max_bytes // 1024**2} MB"
                    break

                target = local_root / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    await sftp.get(full, str(target))
                except (OSError, asyncssh.Error) as exc:
                    skipped.append(f"{rel}: {exc}")
                    continue
                total += size
                pulled.append({"path": rel, "bytes": size})

    return {
        "root": root,
        "files": pulled,
        "bytes": total,
        "skipped": skipped,
        "truncated": truncated,
    }


async def scp_put_many(
    pool: SSHPool,
    devices: list[Device],
    local_path: str | Path,
    remote_path: str,
    *,
    verify: bool = True,
    stream: bool = True,
    concurrency: int | None = None,
) -> list[ScpResult]:
    """Fan out a single file to many devices, optionally verifying sha256."""
    local = Path(local_path)
    sem = asyncio.Semaphore(concurrency or settings.fanout_concurrency)
    expected = _sha256(local) if verify else None

    async def one(d: Device) -> ScpResult:
        host = f"{d.username}@{d.host}"
        res = ScpResult(device_id=d.id, device_name=d.name, host=host, remote_path=remote_path)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        async with sem:
            try:
                conn = await pool.get(d)
                res.bytes_sent = await scp_put(conn, local, remote_path)
                if expected:
                    check = await run_command(conn, f"sha256sum {shlex.quote(remote_path)}")
                    remote_hash = (check.stdout or "").split()[0] if check.stdout.strip() else ""
                    res.checksum_ok = remote_hash == expected
                    if res.checksum_ok is False:
                        res.error = "sha256 mismatch after transfer"
            except (SSHError, OSError, asyncssh.Error) as exc:
                res.error = str(exc)
        res.duration_ms = (loop.time() - t0) * 1000.0

        if stream:
            if res.ok:
                mb = res.bytes_sent / 1e6
                mark = " ✓sha256" if res.checksum_ok else ""
                bus.exec_line(
                    d.id,
                    f"   {d.name}  {local.name}  {mb:.2f} MB  100%  transferred{mark}",
                    "stdout",
                )
            else:
                bus.exec_line(d.id, f"   {d.name}  {local.name}  failed: {res.error}", "stderr")
        return res

    return list(await asyncio.gather(*(one(d) for d in devices)))


def _sha256(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
