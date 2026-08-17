"""Auto-run: execute an operator-supplied schedule script and report on it.

The shape of the problem
------------------------
You have a bash script that runs a dozen projects back to back, unattended, for
hours. What you actually want from a control plane is not a shell — it is
*knowing*: which project is running now, which one broke, and a message on your
phone the moment it does, so an overnight batch that died at 02:10 is not
discovered at 09:00.

So this module is deliberately **not** another command runner. `ssh/commands.py`
already does remote fan-out. This runs **one** script **locally on the machine
hosting this service** (the lab server, where the project directories live),
because that is where a schedule that drives many projects belongs — it is the
conductor, not one of the players.

How progress is known
---------------------
The script is opaque to us, so progress is read out of its stdout in two ways:

1. **Explicit markers** — `::step:: name` / `::step-done:: name rc=0`. Exact, and
   worth the four lines it takes to emit them (see `autorun/example-schedule.sh`).
2. **Heuristic banners** — `=== name ===`, `[3/12] name`, and friends, so a
   script written before this feature existed still gets per-project tracking
   with no edits.

Heuristics switch **off** permanently the first time an explicit marker appears:
a script that emits both should be read the way its author meant, not counted
twice. `markers="strict"` disables them from the start; `"off"` tracks only the
run as a whole.

Regardless of markers, the run-level outcome is always known — it is the
process's exit status — so notifications never depend on the script cooperating.

Lifecycle guarantees
--------------------
* **One run at a time.** These schedules drive the whole fleet: two at once
  would contend for the same GPUs and the same broker queues. A second start
  gets 409.
* **The child gets its own process group**, so stopping kills the whole tree.
  Without it, `^C` would reap `bash` and leave the `python server.py` it
  launched running — the single most annoying way for this to fail.
* **Stop escalates SIGINT → SIGTERM → SIGKILL**, matching `JobRegistry.interrupt`,
  so a run that writes result logs on its way out gets the chance to.
* **State is a folder, not a DB row** (`autorun/runs/<id>/manifest.json` +
  `output.log`), matching how reports are stored: the history survives a
  restart, and reading it needs nothing but a text editor.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import BACKEND_ROOT, settings
from ..ssh.commands import clean_pty_line
from .metrics_bus import bus
from .notify import esc, notifier

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"


class AutoRunError(RuntimeError):
    """Bad request — a missing script, a path outside the sandbox, a second
    concurrent start. Surfaced by the router as a 4xx."""


# --------------------------------------------------------------------- markers
_M_STEP = re.compile(r"^\s*::\s*step\s*::\s*(?P<name>.+?)\s*$", re.I)
_M_DONE = re.compile(
    r"^\s*::\s*step-done\s*::\s*(?P<name>.*?)(?:\s+rc\s*=\s*(?P<rc>-?\d+))?\s*$", re.I
)
_M_NOTE = re.compile(r"^\s*::\s*note\s*::\s*(?P<text>.+?)\s*$", re.I)
_M_FAIL = re.compile(r"^\s*::\s*fail\s*::\s*(?P<text>.+?)\s*$", re.I)
#: Live counters for the current step — `::progress:: batch=128 fps=16.53`.
#: Deliberately **not** notified: a run polling its FPS every 20 s for half an
#: hour would fire ~80 Telegram messages and train you to ignore the channel.
#: It updates the UI and nothing else; `::note::` remains the "tell me" marker.
_M_PROGRESS = re.compile(r"^\s*::\s*progress\s*::\s*(?P<body>.+?)\s*$", re.I)
#: `batch=128` / `fps=16.53` inside a progress line; anything not shaped like a
#: pair stays in `text`.
_KV = re.compile(r"(?P<k>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<v>[^\s]+)")

#: `[3/12] Training DAG` — also tells us how many steps to expect.
_H_COUNTER = re.compile(r"^\s*\[\s*(?P<i>\d+)\s*/\s*(?P<n>\d+)\s*\]\s*(?P<name>.+?)\s*$")
#: `=== name ===`, `--- name`, `### name`, `>>> name`, with optional closing rule.
_H_BANNER = re.compile(
    r"^\s*(?:={2,}|-{3,}|\#{2,}|>{2,})\s*(?P<name>.+?)\s*(?:={2,}|-{3,}|\#{2,}|>{2,})?\s*$"
)

#: Any marker line. Filtered out of the excerpts sent to Telegram: they are the
#: tracking protocol, and quoting them back as "last output" buries the
#: traceback you actually needed under bookkeeping.
_ANY_MARKER = re.compile(r"^\s*::\s*(?:step|step-done|note|fail|progress)\s*::", re.I)

#: A `[3/12]` prefix on a step name — carries the total, wherever it appears.
_COUNTER_PREFIX = re.compile(r"^\[\s*(?P<i>\d+)\s*/\s*(?P<n>\d+)\s*\]\s*(?P<name>.*)$")

#: A banner longer than this is prose that happens to start with dashes, not a
#: project name.
MAX_STEP_NAME = 80


def _banner_name(line: str) -> str | None:
    """The step name in a decorative banner, or None if it is just a rule."""
    m = _H_BANNER.match(line)
    if not m:
        return None
    name = m.group("name").strip(" =-#>_*").strip()
    if not name or len(name) > MAX_STEP_NAME:
        return None
    # A row of `-=-=-=-` has no content; a real banner names something.
    if not re.search(r"[A-Za-z0-9]", name):
        return None
    return name


# ----------------------------------------------------------------------- model
def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {(s % 3600) // 60:02d}m"


@dataclass
class Step:
    """One project inside the schedule."""

    index: int
    name: str
    status: str = "running"  # running | ok | failed | stopped
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    rc: int | None = None
    detail: str = ""
    #: Latest `::progress::` counters, e.g. {"batch": "128", "fps": "16.53"}.
    progress: dict[str, str] = field(default_factory=dict)
    #: The non-`k=v` remainder of that line, for anything unstructured.
    progress_text: str = ""

    @property
    def duration_s(self) -> float:
        # `started_at == 0` means the step is only planned, not begun -- the
        # project queue puts its whole plan on the board up front. Without this
        # a queued row would report the seconds since the epoch as its runtime.
        if not self.started_at:
            return 0.0
        return (self.finished_at or time.time()) - self.started_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "name": self.name,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "rc": self.rc,
            "detail": self.detail,
            "progress": dict(self.progress),
            "progress_text": self.progress_text,
            "duration_s": round(self.duration_s, 1),
        }


@dataclass
class AutoRun:
    """A schedule script, running or finished."""

    id: str
    script: str
    args: list[str] = field(default_factory=list)
    cwd: str = ""
    markers: str = "auto"
    status: str = "starting"  # starting|running|ok|failed|stopped|error
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    exit_code: int | None = None
    error: str = ""
    steps: list[Step] = field(default_factory=list)
    expected_steps: int | None = None
    stalled: bool = False
    lines: int = 0
    log_path: str = ""

    @property
    def running(self) -> bool:
        return self.status in ("starting", "running")

    @property
    def duration_s(self) -> float:
        return (self.finished_at or time.time()) - self.started_at

    @property
    def current(self) -> Step | None:
        return next((s for s in reversed(self.steps) if s.status == "running"), None)

    def counts(self) -> dict[str, int]:
        out = {"total": len(self.steps), "ok": 0, "failed": 0, "running": 0, "stopped": 0}
        for s in self.steps:
            if s.status in out:
                out[s.status] += 1
        return out

    def to_dict(self) -> dict[str, Any]:
        cur = self.current
        return {
            "id": self.id,
            "script": self.script,
            "args": list(self.args),
            "cwd": self.cwd,
            "markers": self.markers,
            "status": self.status,
            "running": self.running,
            "started_at": self.started_at,
            "started_at_iso": datetime.fromtimestamp(self.started_at).isoformat(timespec="seconds"),
            "finished_at": self.finished_at,
            "duration_s": round(self.duration_s, 1),
            "exit_code": self.exit_code,
            "error": self.error,
            "stalled": self.stalled,
            "lines": self.lines,
            "expected_steps": self.expected_steps,
            "current_step": cur.name if cur else "",
            "counts": self.counts(),
            "steps": [s.to_dict() for s in self.steps],
        }


# ---------------------------------------------------------------------- runner
class AutoRunner:
    """Owns the single active schedule and the history on disk."""

    def __init__(self) -> None:
        self._run: AutoRun | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task[None] | None = None
        self._watchdog: asyncio.Task[None] | None = None
        self._tail: deque[str] = deque(maxlen=400)
        self._log: Any = None
        self._log_bytes = 0
        self._last_output = 0.0
        self._notify_steps = True
        self._stopping = False

    # ------------------------------------------------------------------ paths
    @property
    def root(self) -> Path:
        return settings.autorun_path

    @property
    def runs_dir(self) -> Path:
        return self.root / "runs"

    def resolve_script(self, script: str) -> Path:
        """Locate a schedule script, refusing anything outside `AUTORUN_DIR`.

        The sandbox is the whole security story for this endpoint: it executes
        a shell script with this service's privileges, so *which file* is the
        only thing worth constraining. Paths are resolved before the check, so
        `../` and a symlink pointing out of the directory are both caught.
        `AUTORUN_ALLOW_ANY_PATH=true` lifts it for a single-operator box.
        """
        raw = (script or "").strip()
        if not raw:
            raise AutoRunError("no script given")

        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = self.root / candidate

        try:
            resolved = candidate.resolve()
        except OSError as exc:
            raise AutoRunError(f"cannot resolve {raw!r}: {exc}") from exc

        if not settings.autorun_allow_any_path:
            root = self.root.resolve()
            if not (resolved == root or root in resolved.parents):
                raise AutoRunError(
                    f"{raw!r} is outside AUTORUN_DIR ({root}). Move the script there, "
                    "or set AUTORUN_ALLOW_ANY_PATH=true."
                )

        if not resolved.exists():
            raise AutoRunError(f"no such script: {resolved}")
        if not resolved.is_file():
            raise AutoRunError(f"not a file: {resolved}")
        return resolved

    def list_scripts(self) -> list[dict[str, Any]]:
        """Everything runnable in the sandbox — populates a picker in the UI."""
        if not self.root.exists():
            return []
        out = []
        for p in sorted(self.root.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in (".sh", ".bash"):
                continue
            if self.runs_dir in p.parents:  # captured logs, not schedules
                continue
            stat = p.stat()
            out.append(
                {
                    "name": p.relative_to(self.root).as_posix(),
                    "path": str(p),
                    "size": stat.st_size,
                    "modified": stat.st_mtime,
                }
            )
        return out

    # ------------------------------------------------------------------ state
    def status(self) -> dict[str, Any]:
        return {
            "active": self._run.to_dict() if self._run else None,
            "running": bool(self._run and self._run.running),
            "notify": notifier.status(),
            "stall_after_s": settings.autorun_stall_seconds,
            "dir": str(self.root),
        }

    def tail(self, limit: int = 200) -> list[str]:
        return list(self._tail)[-max(1, min(limit, self._tail.maxlen or 400)):]

    def history(self, limit: int = 25) -> list[dict[str, Any]]:
        """Past runs, newest first, read back from their manifests."""
        if not self.runs_dir.exists():
            return []
        out = []
        for d in sorted(self.runs_dir.iterdir(), reverse=True):
            manifest = d / "manifest.json"
            if not manifest.is_file():
                continue
            try:
                out.append(json.loads(manifest.read_text("utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
            if len(out) >= max(1, min(200, limit)):
                break
        return out

    def read_log(self, run_id: str, limit: int = 500) -> dict[str, Any]:
        d = self.runs_dir / run_id
        path = d / "output.log"
        if not path.is_file():
            raise AutoRunError(f"no log for run {run_id!r}")
        lines = path.read_text("utf-8", "replace").splitlines()
        return {"id": run_id, "total_lines": len(lines), "lines": lines[-max(1, limit):]}

    # ------------------------------------------------------------------ start
    async def start(
        self,
        script: str,
        *,
        args: list[str] | None = None,
        cwd: str | None = None,
        markers: str = "auto",
        notify: bool = True,
        notify_steps: bool = True,
        env: dict[str, str] | None = None,
    ) -> AutoRun:
        if self._run and self._run.running:
            raise AutoRunError(
                f"auto-run {self._run.id} is already running ({self._run.script}). "
                "Stop it first — these schedules drive the whole fleet."
            )
        # The other half of the same lock. A project queue drives the same
        # fleet over SSH; a script starting beside it would put two servers on
        # the same broker and silently ruin both sets of numbers. Imported here
        # rather than at module scope because `project_queue` imports this one.
        from .project_queue import runner as queue_runner

        if queue_runner.active is not None:
            raise AutoRunError(
                "a project queue is already running. Stop it first — "
                "these schedules drive the whole fleet."
            )

        path = self.resolve_script(script)
        shell = _find_bash()
        if not shell:
            raise AutoRunError(
                "no `bash` on PATH. Install it (Linux: it is already there; "
                "Windows: Git Bash or WSL) — schedule scripts are bash."
            )

        workdir = Path(cwd).expanduser() if cwd else path.parent
        if not workdir.is_dir():
            raise AutoRunError(f"working directory does not exist: {workdir}")

        if markers not in ("auto", "strict", "off"):
            raise AutoRunError(f"markers must be auto|strict|off, not {markers!r}")

        run_id = f"{datetime.now().strftime('%y%m%d-%H%M%S')}-{path.stem[:24]}"
        outdir = self.runs_dir / run_id
        outdir.mkdir(parents=True, exist_ok=True)

        run = AutoRun(
            id=run_id,
            script=str(path),
            args=list(args or []),
            cwd=str(workdir),
            markers=markers,
            log_path=str(outdir / "output.log"),
        )

        child_env = os.environ.copy()
        # Unbuffered, or a nested `python train.py` withholds its output until it
        # exits and every marker arrives hours late, all at once.
        child_env["PYTHONUNBUFFERED"] = "1"
        child_env["AUTORUN_ID"] = run_id
        child_env["AUTORUN_DIR"] = str(outdir)
        # Config the script needs but the API never sees (fleet credentials).
        child_env.update(env_passthrough())
        if env:
            child_env.update({str(k): str(v) for k, v in env.items()})

        try:
            proc = await asyncio.create_subprocess_exec(
                shell,
                str(path),
                *run.args,
                cwd=str(workdir),
                stdout=asyncio.subprocess.PIPE,
                # Merged, not separate: interleaving order is what makes the
                # transcript readable and puts a traceback under the step that
                # raised it.
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL,
                env=child_env,
                **_process_group_kwargs(),
            )
        except OSError as exc:
            raise AutoRunError(f"could not launch {path.name}: {exc}") from exc

        self._run = run
        self._proc = proc
        self._tail = deque(maxlen=400)
        self._log_bytes = 0
        self._last_output = time.time()
        self._notify_steps = notify_steps
        self._stopping = False
        run.status = "running"

        try:
            self._log = open(outdir / "output.log", "w", encoding="utf-8", buffering=1)
        except OSError as exc:  # a lost transcript must not lose the run
            log.warning("autorun %s: cannot open log (%s)", run_id, exc)
            self._log = None

        self._save(run)
        bus.exec_line("", f"▶ auto-run {run_id}: {path.name}", "meta")
        bus.event("autorun_started", run=run.to_dict())

        if notify:
            await notifier.send(
                f"▶️ <b>Auto-run started</b>\n"
                f"<code>{esc(path.name)}</code>\n"
                f"{esc(datetime.fromtimestamp(run.started_at).strftime('%Y-%m-%d %H:%M'))}"
                f" · run <code>{esc(run_id)}</code>"
            )

        self._task = asyncio.create_task(self._pump(run, notify=notify))
        self._watchdog = asyncio.create_task(self._watch_stall(run, notify=notify))
        return run

    # -------------------------------------------------------------------- pump
    async def _pump(self, run: AutoRun, *, notify: bool) -> None:
        """Read the script's output to EOF, tracking steps as it goes."""
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        heuristics = run.markers == "auto"

        try:
            while True:
                try:
                    raw = await proc.stdout.readline()
                except (asyncio.LimitOverrunError, ValueError):
                    # A single line longer than the stream limit (a script
                    # dumping a binary blob). Skip it rather than dying.
                    continue
                if not raw:
                    break

                line = clean_pty_line(raw.decode("utf-8", "replace"))
                self._last_output = time.time()
                run.lines += 1
                self._record(line)

                if run.markers != "off":
                    heuristics = await self._interpret(run, line, heuristics, notify=notify)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - never leave a run wedged in "running"
            log.exception("autorun %s: output pump failed", run.id)

        rc = await proc.wait()
        await self._finish(run, rc, notify=notify)

    def _record(self, line: str) -> None:
        self._tail.append(line)
        bus.exec_line("", f"│  {line}", "stdout")
        if self._log is None:
            return
        cap = settings.autorun_log_max_mb * 1024 * 1024
        if self._log_bytes >= cap:
            return
        try:
            self._log.write(line + "\n")
            self._log_bytes += len(line) + 1
            if self._log_bytes >= cap:
                self._log.write(f"\n… log truncated at {settings.autorun_log_max_mb} MB\n")
        except OSError:
            pass

    async def _interpret(
        self, run: AutoRun, line: str, heuristics: bool, *, notify: bool
    ) -> bool:
        """Apply one line to the step model. Returns whether heuristics stay on."""
        if m := _M_STEP.match(line):
            self._begin_step(run, m.group("name"))
            return False  # explicit markers win from here on

        if m := _M_DONE.match(line):
            rc_raw = m.group("rc")
            await self._end_step(
                run,
                rc=int(rc_raw) if rc_raw is not None else 0,
                name=m.group("name").strip(),
                notify=notify,
            )
            return False

        if m := _M_PROGRESS.match(line):
            self._apply_progress(run, m.group("body"))
            return False

        if m := _M_NOTE.match(line):
            text = m.group("text")
            bus.event("autorun_note", run_id=run.id, text=text)
            if notify and settings.notify_notes:
                await notifier.send(f"ℹ️ {esc(text)}", silent=True)
            return False

        if m := _M_FAIL.match(line):
            await self._end_step(run, rc=1, detail=m.group("text"), notify=notify)
            return False

        if not heuristics:
            return heuristics

        if m := _H_COUNTER.match(line):
            run.expected_steps = int(m.group("n"))
            self._begin_step(run, m.group("name"))
            return True

        if name := _banner_name(line):
            self._begin_step(run, name)
        return True

    def _apply_progress(self, run: AutoRun, body: str) -> None:
        """Update the open step's live counters and push them to the UI.

        Not persisted on every tick: these arrive every few seconds for hours,
        and rewriting the manifest each time would be the only disk activity a
        long run has. The counters are ephemeral by nature — the transcript and
        the per-step result are what outlive the run.
        """
        step = run.current
        if step is None:
            return
        pairs = {m.group("k"): m.group("v") for m in _KV.finditer(body)}
        leftover = _KV.sub("", body).strip(" ·|,")
        if pairs:
            step.progress.update(pairs)
        if leftover:
            step.progress_text = leftover[:200]
        bus.event(
            "autorun_progress",
            run_id=run.id,
            step=step.index,
            # `step_name`, not `name`: `MetricsBus.event()` takes the event name
            # positionally, so a `name` field collides with it.
            step_name=step.name,
            progress=dict(step.progress),
            text=step.progress_text,
        )

    def _begin_step(self, run: AutoRun, name: str) -> None:
        """Open a step, closing any still-open predecessor.

        A previous step with no explicit `::step-done::` is recorded as `ok`:
        the script moved on to the next project, and a script that aborts on
        failure would not have. The run's own exit code stays authoritative.
        """
        name = name.strip()
        # `::step:: [2/4] DAG` — take the total from the prefix and drop it from
        # the label, so the name stays the project and the count is tracked
        # once, in the field meant for it.
        if m := _COUNTER_PREFIX.match(name):
            run.expected_steps = int(m.group("n"))
            name = m.group("name").strip() or name
        name = name[:MAX_STEP_NAME]
        if not name:
            return
        prev = run.current
        if prev is not None:
            if prev.name == name:  # a repeated banner, not a new project
                return
            prev.status = "ok"
            prev.finished_at = time.time()

        step = Step(index=len(run.steps) + 1, name=name)
        run.steps.append(step)
        total = f"/{run.expected_steps}" if run.expected_steps else ""
        bus.exec_line("", f"┌─ step {step.index}{total}: {name}", "meta")
        bus.event("autorun_step", run_id=run.id, step=step.to_dict())
        self._save(run)

    async def _end_step(
        self,
        run: AutoRun,
        *,
        rc: int,
        name: str = "",
        detail: str = "",
        notify: bool = True,
    ) -> None:
        step = run.current
        if step is None:
            # `::step-done::` with no open step — synthesise one so the result
            # is not silently dropped.
            if m := _COUNTER_PREFIX.match(name):
                run.expected_steps = int(m.group("n"))
                name = m.group("name").strip()
            step = Step(index=len(run.steps) + 1, name=name or f"step {len(run.steps) + 1}")
            run.steps.append(step)

        step.status = "ok" if rc == 0 else "failed"
        step.finished_at = time.time()
        step.rc = rc
        if detail:
            step.detail = detail[:500]

        mark = "✔" if step.status == "ok" else "✖"
        bus.exec_line(
            "",
            f"└─ {mark} {step.name} ({_fmt_duration(step.duration_s)}, rc={rc})",
            "stdout" if rc == 0 else "stderr",
        )
        bus.event("autorun_step", run_id=run.id, step=step.to_dict())
        self._save(run)

        if not notify:
            return
        if step.status == "failed":
            # Loud: this is the message the whole feature exists for.
            await notifier.send(self._failure_message(run, step))
        elif self._notify_steps:
            # `step.index`, not the success count: after a failure the two
            # diverge, and "step 2/4" for the third project is just wrong.
            total = f"/{run.expected_steps}" if run.expected_steps else ""
            await notifier.send(
                f"✔️ <b>{esc(step.name)}</b> done in {_fmt_duration(step.duration_s)}\n"
                f"<i>step {step.index}{total} · {esc(Path(run.script).name)}</i>",
                silent=True,
            )

    def _failure_message(self, run: AutoRun, step: Step) -> str:
        tail = "\n".join(self._tail_lines(12))
        body = (
            f"❌ <b>Step failed — {esc(step.name)}</b>\n"
            f"exit <b>{step.rc}</b> · after {_fmt_duration(step.duration_s)}\n"
            f"<code>{esc(Path(run.script).name)}</code> · run <code>{esc(run.id)}</code>"
        )
        if step.detail:
            body += f"\n{esc(step.detail)}"
        if tail:
            body += f"\n\n<b>last output</b>\n<pre>{esc(tail)}</pre>"
        return body

    def _tail_lines(self, n: int) -> list[str]:
        """Recent *real* output — markers stripped.

        Quoting the tracking protocol back at you as "last output" pushes the
        traceback that actually explains the failure off the top of the message.
        """
        useful = [ln for ln in self._tail if ln.strip() and not _ANY_MARKER.match(ln)]
        return useful[-n:]

    # ------------------------------------------------------------------ stall
    async def _watch_stall(self, run: AutoRun, *, notify: bool) -> None:
        """Warn when a run goes quiet for too long.

        A long training step is legitimately silent, so this is a *warning*, not
        a kill: the threshold is generous and the run is left alone. It fires
        once per quiet period and reports again when output resumes.
        """
        limit = settings.autorun_stall_seconds
        if limit <= 0:
            return
        try:
            while run.running:
                await asyncio.sleep(min(30.0, max(5.0, limit / 4)))
                quiet = time.time() - self._last_output
                if quiet >= limit and not run.stalled:
                    run.stalled = True
                    self._save(run)
                    cur = run.current
                    where = f" during <b>{esc(cur.name)}</b>" if cur else ""
                    bus.exec_line("", f"⏳ no output for {_fmt_duration(quiet)}", "meta")
                    bus.event("autorun_stalled", run_id=run.id, quiet_s=round(quiet, 1))
                    if notify:
                        await notifier.send(
                            f"⏳ <b>Auto-run quiet</b>{where}\n"
                            f"No output for {_fmt_duration(quiet)} — still running, not stopped.\n"
                            f"<code>{esc(run.id)}</code>"
                        )
                elif quiet < limit and run.stalled:
                    run.stalled = False
                    self._save(run)
                    bus.exec_line("", "▸ output resumed", "meta")
                    if notify:
                        await notifier.send("▶️ Output resumed.", silent=True)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the watchdog must not kill the run
            log.exception("autorun %s: stall watchdog failed", run.id)

    # ----------------------------------------------------------------- finish
    async def _finish(self, run: AutoRun, rc: int, *, notify: bool) -> None:
        open_step = run.current
        if open_step is not None:
            open_step.finished_at = time.time()
            if self._stopping:
                open_step.status = "stopped"
            elif rc == 0:
                open_step.status = "ok"
            else:
                open_step.status = "failed"
                open_step.rc = rc

        run.finished_at = time.time()
        run.exit_code = rc
        if self._stopping:
            run.status = "stopped"
        else:
            run.status = "ok" if rc == 0 else "failed"

        if self._watchdog:
            self._watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._watchdog
            self._watchdog = None

        if self._log is not None:
            with contextlib.suppress(OSError):
                self._log.close()
            self._log = None

        self._save(run)
        mark = {"ok": "✔", "failed": "✖", "stopped": "■"}.get(run.status, "•")
        bus.exec_line(
            "",
            f"└─ {mark} auto-run {run.status} (exit {rc}, {_fmt_duration(run.duration_s)})",
            "stdout" if run.status == "ok" else "stderr",
        )
        bus.event("autorun_finished", run=run.to_dict())

        if notify:
            await notifier.send(self._summary_message(run))

    def _summary_message(self, run: AutoRun) -> str:
        c = run.counts()
        head = {
            "ok": "✅ <b>Auto-run finished</b>",
            "failed": "❌ <b>Auto-run FAILED</b>",
            "stopped": "■ <b>Auto-run stopped</b>",
        }.get(run.status, "• <b>Auto-run ended</b>")

        body = (
            f"{head}\n"
            f"<code>{esc(Path(run.script).name)}</code> · {_fmt_duration(run.duration_s)}"
            f" · exit <b>{run.exit_code}</b>\n"
        )
        if c["total"]:
            body += f"{c['ok']}/{c['total']} steps ok"
            if c["failed"]:
                body += f" · <b>{c['failed']} failed</b>"
            body += "\n"

            rows = []
            for s in run.steps:
                icon = {"ok": "✔", "failed": "✖", "stopped": "■"}.get(s.status, "…")
                rows.append(f"{icon} {s.name[:34]:<34} {_fmt_duration(s.duration_s)}")
            body += f"<pre>{esc(chr(10).join(rows))}</pre>"

        failed = [s for s in run.steps if s.status == "failed"]
        if failed:
            # The tail here is the *end* of the run, which after a mid-schedule
            # failure is whatever succeeded afterwards -- quoting it under a
            # "FAILED" headline points at the wrong project. The step table
            # already says which one broke, and its own alert carried the
            # traceback when it happened.
            body += "\nfailed: " + esc(", ".join(s.name for s in failed[:6]))
            if len(failed) > 6:
                body += f" (+{len(failed) - 6} more)"
        elif run.status != "ok":
            # Nothing localised the failure -- the script died without closing a
            # step -- so the transcript's tail is the only evidence there is.
            tail = "\n".join(self._tail_lines(12))
            if tail:
                body += f"\n<b>last output</b>\n<pre>{esc(tail)}</pre>"
        return body

    # ------------------------------------------------------------------- stop
    async def stop(self, *, grace: float | None = None) -> dict[str, Any]:
        """SIGINT → SIGTERM → SIGKILL against the whole process group."""
        run, proc = self._run, self._proc
        if run is None or not run.running or proc is None:
            return {"stopped": False, "note": "nothing is running"}

        self._stopping = True
        wait = settings.autorun_stop_grace if grace is None else grace
        bus.exec_line("", f"^C  auto-run {run.id}", "meta")

        outcome = "already exited"
        for sig, label in _stop_ladder():
            if proc.returncode is not None:
                break
            _signal_group(proc, sig)
            outcome = label
            try:
                await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=wait)
                break
            except asyncio.TimeoutError:
                continue

        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.kill()
            outcome = "killed"

        if self._task:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(asyncio.shield(self._task), timeout=10)

        return {"stopped": True, "outcome": outcome, "run": run.to_dict()}

    # ------------------------------------------------------------- persistence
    def _save(self, run: AutoRun) -> None:
        """Write the manifest. Best-effort: losing it must not stop the run."""
        try:
            d = self.runs_dir / run.id
            d.mkdir(parents=True, exist_ok=True)
            tmp = d / "manifest.json.tmp"
            tmp.write_text(json.dumps(run.to_dict(), indent=2), encoding="utf-8")
            tmp.replace(d / "manifest.json")
        except OSError as exc:
            log.warning("autorun %s: manifest not saved (%s)", run.id, exc)

    async def aclose(self) -> None:
        """Shutdown hook: stop a schedule rather than orphan it."""
        if self._run and self._run.running:
            log.info("autorun: stopping %s for shutdown", self._run.id)
            with contextlib.suppress(Exception):
                await self.stop()


# ------------------------------------------------------------ env passthrough
def env_passthrough() -> dict[str, str]:
    """`.env` keys a schedule may inherit, filtered by `AUTORUN_ENV_PREFIXES`.

    Read from the file rather than `os.environ` because `pydantic-settings`
    loads `.env` into the `Settings` object and nowhere else -- so a child
    process started by this service sees none of it unless uvicorn itself was
    launched with those variables exported.

    Only prefixed keys are passed. The API token and the bot token stay behind:
    a script that runs `set -x`, dumps `env` on failure, or simply crashes
    would otherwise print them into a transcript this service stores and
    forwards to chat.
    """
    prefixes = tuple(
        p.strip() for p in (settings.autorun_env_prefixes or "").split(",") if p.strip()
    )
    if not prefixes:
        return {}

    path = BACKEND_ROOT / ".env"
    out: dict[str, str] = {}
    try:
        text = path.read_text("utf-8", "replace")
    except OSError:
        return out

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key.startswith(prefixes):
            continue
        value = value.strip()
        # Tolerate quoted values; a password ending in "." does not need them,
        # but an operator may well add them.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


# ------------------------------------------------------------------- platform
def _find_bash() -> str | None:
    found = shutil.which("bash")
    if found:
        return found
    if IS_WINDOWS:  # dev boxes: Git Bash is not always on PATH
        for guess in (
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files (x86)\Git\bin\bash.exe",
        ):
            if Path(guess).is_file():
                return guess
    return None


def _process_group_kwargs() -> dict[str, Any]:
    """Put the child in its own group so stopping reaches its grandchildren.

    Without this, signalling `bash` leaves the `python server.py` it started
    running — the schedule looks stopped while still holding the GPU.
    """
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _stop_ladder() -> list[tuple[int, str]]:
    if IS_WINDOWS:
        return [(signal.CTRL_BREAK_EVENT, "ctrl-break"), (signal.SIGTERM, "terminated")]
    return [(signal.SIGINT, "interrupted"), (signal.SIGTERM, "terminated")]


def _signal_group(proc: asyncio.subprocess.Process, sig: int) -> None:
    try:
        if IS_WINDOWS:
            proc.send_signal(sig)
        else:
            os.killpg(os.getpgid(proc.pid), sig)
    except (ProcessLookupError, PermissionError, OSError) as exc:
        log.debug("autorun: signal %s failed (%s)", sig, exc)


#: Process-wide singleton.
runner = AutoRunner()
