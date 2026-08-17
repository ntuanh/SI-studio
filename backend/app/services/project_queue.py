"""Run every project back to back, over the Control tab's own SSH path.

The shape of the problem
------------------------
Running one project by hand is three gestures on the Control tab, and they are
the same three gestures every time:

    select the control server            -> Run   `python3 server.py`
    select all of stage 1 (the edges)    -> Run   `python3 client.py --layer_id 1`
    select all of stage 2 (the clouds)   -> Run   `python3 client.py --layer_id 2`

The *only* thing that differs between one project and the next is the working
directory those three commands run in. Six projects is eighteen gestures and one
transposed directory away from a wasted afternoon, which is a bad use of an
operator and a worse use of a fleet.

So this module is the queue: a list of directories, one button, and the same
status board the schedule scripts already report to.

Why it is not a schedule script
-------------------------------
`services/autorun.py` runs an operator-supplied bash script as a *local*
subprocess. That is right for a script like `autorun/fleet-3project.sh`, which
encodes each project's own quirks -- PA needs `server.clients = [9, 9]`, dmsf
needs `--device cpu`, the launch order differs per project -- and needs a real
language to say them in.

This is the other half: the projects that differ *only* by directory, which is
most of them. Writing bash to express "the same three commands, six times" is
work the UI can simply not require. Both feed the same Progress board, because
from the outside they are the same question -- which project is up, how far in,
did anything break.

Why it reuses `ssh/commands.py` rather than opening its own sessions
--------------------------------------------------------------------
Because the operator already connected. The pool holds those sessions, the jump
host is already configured, and the stored credentials are already resolved.
Dialling again from a second code path would mean a second set of connection
bugs, a second place jump-host support has to be remembered, and a second answer
to "is device-3 up". `start_job`/`fan_out_jobs` are exactly what the Control tab
presses, so a project launched from here is byte-for-byte a project launched by
hand -- including the pty, the live `exec_line` output, and `^C` still working.

How a project is known to be finished
--------------------------------------
The server exits. `server.py` ends when the video drains, so its exec job
finishing *is* the completion signal -- no log parsing, no guessed timeout, and
nothing that goes stale when a log line is reworded. `autorun/fleet-3project.sh`
already assumes exactly this ("server exited after ${waited}s").

`budget_s` is a backstop, not a policy: when it expires the queue stops
*waiting* and says so. It never kills the run -- the archive is written at
shutdown, so killing here would destroy the results the queue exists to collect.

Progress
--------
Four sources, in falling order of honesty, and the bar uses the best one it has:

1. `batch`/`total` scraped from the server's own output -- a measurement.
2. elapsed vs `expected_s` -- an estimate the operator supplied.
3. phase 1..3 of launch -- structural, always available, always true.

with the queue-level bar (projects done / projects planned) known up front,
because the plan is known up front. Nothing here invents a denominator: a bar
without an honest one is worse than no bar, which is why the schedule scripts
only ever get one when they say `total=`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlmodel import select

from ..config import settings
from ..models import CommandPreset, Device, QueueProject, ServerConfig
from ..ssh import commands as cmds
from ..ssh import gateway
from ..ssh.pool import pool
from .autorun import AutoRun, Step, runner as script_runner
from .metrics_bus import bus
from .notify import esc, notifier
from .server_state import server_state

log = logging.getLogger(__name__)


class QueueError(RuntimeError):
    """Refused before anything was launched."""


#: Fallback when no `budget_s` is given. Generous on purpose: the budget only
#: decides when the queue stops *waiting*, and cutting a slow project short
#: costs a whole run while waiting too long costs some minutes.
DEFAULT_BUDGET_S = 3600

#: How long to let a launch settle before deciding it detached. The clients are
#: meant to keep running, so this is only long enough to catch the ones that
#: fail immediately (`No such file or directory`, a bad interpreter).
LAUNCH_SETTLE_S = 6.0

#: Grace after the server exits, before the stragglers are counted. Clients
#: normally exit on their own once the server sends STOP; this is the window
#: they get to do it in.
DRAIN_GRACE_S = 12.0

#: How often the wait loop re-publishes the open project's counters.
POLL_S = 5.0

#: Everything this shuts down between projects. Matches `kill_fleet` in
#: `autorun/fleet-3project.sh` -- two servers would both bind `rpc_queue`, and
#: the next project's clients would register into the previous run's topology.
#:
#: Deliberately flat: no quoted string wraps the `$( )`, because this crosses
#: two parsers (python's literal, then the remote shell) and a `\"` inside an
#: outer `"..."` survived neither intact in the fleet script -- `pgrep` was
#: handed a mangled argument and the count came back empty. The output has no
#: spaces in it, so the outer quotes bought nothing and cost the field.
CLEANUP_COMMAND = (
    'pkill -f "client.py --layer_id" 2>/dev/null; '
    'pkill -f "server.py" 2>/dev/null; '
    "sleep 1; "
    'echo left=$(pgrep -fc "client.py --layer_id|server.py" 2>/dev/null || echo 0)'
)

#: Counters worth lifting out of the server's stdout. Deliberately a handful of
#: shapes rather than a parser: these are the lines every one of these projects
#: already prints, and a miss costs a nicety, never correctness.
_BATCH_RE = re.compile(r"\b(?:batch|frame|step)\s*[:=#]?\s*(\d+)\s*(?:/\s*(\d+))?", re.I)
_FPS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*fps\b", re.I)
_REG_RE = re.compile(r"\b(?:registered|REGISTER\w*)\b[^0-9]{0,12}(\d+)\s*(?:/\s*(\d+))?", re.I)
_TOTAL_RE = re.compile(r"\b(?:total|frames|of)\s*[:=]?\s*(\d+)\s*(?:frames)?\b", re.I)


@dataclass
class Target:
    """One phase's worth of hosts, resolved once at start.

    Devices are copied out of the session rather than held: this run outlives
    the request that started it by hours, and a detached ORM instance is a
    lazy-load away from raising in a background task.
    """

    key: str          # `__server__`, or a stage id
    label: str        # "Control server", "Edge", "Stage 2"
    devices: list[Device]
    command: str


@dataclass
class Project:
    name: str
    path: str
    expected_s: int = 0
    overrides: dict[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------- runner
class ProjectQueueRunner:
    """Owns the single active queue.

    One at a time, for the same reason a schedule script is: these drive the
    whole fleet, and two would contend for the same GPUs and broker queues. The
    lock is shared with `services/autorun.py` in both directions -- see
    `_refuse_if_busy`.
    """

    def __init__(self) -> None:
        self._run: AutoRun | None = None
        self._task: asyncio.Task[None] | None = None
        self._targets: list[Target] = []
        self._projects: list[Project] = []
        self._jobs: list[cmds.ExecJob] = []
        self._stopping = False
        self._budget_s = DEFAULT_BUDGET_S
        self._cleanup = True
        self._notify = True
        self._notify_steps = True

    # ------------------------------------------------------------------ state
    @property
    def active(self) -> AutoRun | None:
        return self._run if (self._run and self._run.running) else None

    def status(self) -> dict[str, Any]:
        return {
            "active": self._run.to_dict() if self._run else None,
            "notify": notifier.status(),
            "budget_s": self._budget_s,
            "targets": [
                {"key": t.key, "label": t.label, "command": t.command,
                 "devices": [d.id for d in t.devices]}
                for t in self._targets
            ],
        }

    def _refuse_if_busy(self) -> None:
        if self.active is not None:
            raise QueueError("a project queue is already running")
        # The other half of the same lock. A schedule script drives the same
        # fleet; letting a queue start beside it would put two servers on the
        # same broker and silently ruin both sets of numbers.
        if (script_runner.status().get("active") or {}).get("running"):
            raise QueueError(
                "a schedule script is already running; stop it on the Progress tab first"
            )

    # ------------------------------------------------------------------ start
    async def start(
        self,
        *,
        only: list[str] | None = None,
        cleanup: bool = True,
        budget_s: int = 0,
        notify: bool = True,
        notify_steps: bool = True,
    ) -> AutoRun:
        """Resolve the plan, then launch it in the background.

        Everything that can be refused is refused *here*, while an HTTP request
        is still listening: a missing server login, an empty project list, a
        stage with no command saved. A queue that fails at project four because
        stage 2 never had a preset is a queue that wasted twenty minutes to
        report a typo.
        """
        self._refuse_if_busy()

        projects, targets = await self._resolve(only or [])

        run = AutoRun(
            id=f"q{uuid.uuid4().hex[:8]}",
            script="(project queue)",
            args=[p.name for p in projects],
            markers="off",
            status="starting",
            expected_steps=len(projects),
        )
        # Every project is on the board before the first one starts. The plan is
        # known up front, so showing it one row at a time would be hiding it --
        # and it is what makes "2 of 6" mean anything while project 2 is open.
        for i, p in enumerate(projects, start=1):
            run.steps.append(
                Step(
                    index=i,
                    name=p.name,
                    status="queued",
                    started_at=0.0,   # planned, not begun — see Step.duration_s
                    progress={"phase": "0", "phases": str(len(targets))},
                    progress_text=p.path,
                )
            )

        self._run = run
        self._projects = projects
        self._targets = targets
        self._jobs = []
        self._stopping = False
        self._budget_s = budget_s or DEFAULT_BUDGET_S
        self._cleanup = cleanup
        self._notify = notify
        self._notify_steps = notify_steps

        bus.exec_line("", f"▶ queue {run.id}: {len(projects)} project(s)", "meta")
        bus.event("autorun_started", run=run.to_dict())

        if notify:
            await notifier.send(
                "▶️ <b>Project queue started</b>\n"
                f"{len(projects)} project(s): {esc(', '.join(p.name for p in projects))}",
                silent=True,
            )

        self._task = asyncio.create_task(self._drive(), name=f"queue-{run.id}")
        return run

    # --------------------------------------------------------------- planning
    async def _resolve(self, only: list[str]) -> tuple[list[Project], list[Target]]:
        """Turn the saved rows into a plan, or explain why there isn't one."""
        from ..db import SessionFactory

        async with SessionFactory() as session:
            rows = (
                await session.exec(select(QueueProject).order_by(QueueProject.position))
            ).all()
            # Naming a project explicitly overrides its `enabled` tick: asking
            # for it *is* enabling it for this run, and refusing on the grounds
            # of a checkbox the caller just bypassed would only be confusing.
            wanted = {n.strip().lower() for n in only if n.strip()}
            if wanted:
                chosen = [r for r in rows if (r.name or "").strip().lower() in wanted]
            else:
                chosen = [r for r in rows if r.enabled]

            projects = [
                Project(
                    name=r.name or r.path,
                    path=r.path,
                    expected_s=r.expected_s or 0,
                    overrides=dict(r.overrides or {}),
                )
                for r in chosen
            ]
            if not projects:
                raise QueueError(
                    "no projects to run — add one on the Progress tab (⚙ Edit projects)"
                )

            presets = {
                _norm(p.label): p.command
                for p in (await session.exec(select(CommandPreset))).all()
            }
            devices = (await session.exec(select(Device))).all()

            cfg = await session.get(ServerConfig, 1)
            if cfg is None or not gateway.is_configured(cfg):
                raise QueueError(
                    "the control server has no SSH host/username configured; "
                    "fill in the Broker / backend server card on Control first"
                )
            targets = [
                Target(
                    key=gateway.SERVER_DEVICE_ID,
                    label=gateway.SERVER_DEVICE_NAME,
                    devices=[gateway.server_device(cfg)],
                    command=_preset(presets, ["run server"]),
                )
            ]
            targets.extend(_stage_targets(devices, presets))

        if missing := [t.label for t in targets if not t.command]:
            # Named rather than implied: the fix is one chip on the Control tab,
            # and "no command for Stage 2" says which one.
            raise QueueError(
                "no command saved for: " + ", ".join(missing) +
                " — save it on Control as “run server” / “run stage <n>”"
            )
        if len(targets) < 2:
            raise QueueError("no devices in any stage; add them on the Devices tab")
        return projects, targets

    # ---------------------------------------------------------------- driving
    async def _drive(self) -> None:
        run = self._run
        assert run is not None
        run.status = "running"
        try:
            for step in run.steps:
                if self._stopping:
                    step.status = "stopped"
                    self._emit_step(step)
                    continue
                project = self._projects[step.index - 1]
                await self._run_project(project, step)
        except asyncio.CancelledError:
            run.error = "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001 - a queue must report, not vanish
            log.exception("project queue failed")
            run.error = str(exc)
            self._log(f"✗ queue failed: {exc}", "stderr")
        finally:
            await self._finish()

    async def _run_project(self, project: Project, step: Step) -> None:
        run = self._run
        assert run is not None
        step.status = "running"
        step.started_at = time.time()
        step.detail = project.path
        self._emit_step(step)
        bus.exec_line(
            "", f"┌─ project {step.index}/{len(run.steps)}: {project.name}  ({project.path})",
            "meta",
        )
        if self._notify and self._notify_steps:
            await notifier.send(
                f"▶️ <b>{esc(project.name)}</b> starting\n<code>{esc(project.path)}</code>",
                silent=True,
            )

        if self._cleanup:
            await self._cleanup_fleet(step)

        server_job = None
        for phase, target in enumerate(self._targets, start=1):
            if self._stopping:
                break
            command = project.overrides.get(target.key) or target.command
            step.progress["phase"] = str(phase)
            step.progress["phases"] = str(len(self._targets))
            step.progress_text = f"starting {target.label}"
            self._emit_progress(step)

            job = await self._launch(
                target, command, project.path, step, watch=(phase == 1)
            )
            if phase == 1:
                server_job = job
                if job is None or (job.result is not None and not job.result.ok):
                    # No server, no run. Everything after this would register
                    # into nothing and hang until the budget expired.
                    err = (
                        (job.result.error or f"exit {job.result.exit}")
                        if job and job.result else "failed to start"
                    )
                    await self._fail(step, f"server did not start: {err}")
                    return

        if self._stopping:
            step.status = "stopped"
            self._emit_step(step)
            return

        step.progress_text = "running"
        self._emit_progress(step)
        await self._await_server(server_job, project, step)

    async def _launch(
        self, target: Target, command: str, cwd: str, step: Step, *, watch: bool = False
    ) -> cmds.ExecJob | None:
        """Start one phase's command on its hosts and leave it running.

        Same call the Control tab makes, `cd`-wrapped the same way: the working
        directory travels with the command because each one gets its own shell
        (see `with_working_directory`).

        `watch` attaches the counter scraper. Only the server gets it: the
        clients print the same shapes, and nine of them writing the same three
        keys would make the board flicker between whichever host spoke last.
        """
        # `$BROKER_IP` is resolved here for the same reason the exec router
        # resolves it: these are the operator's own saved presets, and one that
        # works when pressed on Control must not reach the shell as a literal
        # when the queue presses it. `fan_out` does its own, so the cleanup
        # sweep does not need this.
        resolved = cmds.substitute_broker_ip(command, server_state.host)
        full = cmds.with_working_directory(resolved, cwd)
        bus.exec_line("", f"$ {full}   → {len(target.devices)} host(s)", "meta")

        # Started concurrently under the same semaphore `fan_out_jobs` uses, so
        # nine edges come up the way they do when the Control tab's *select all*
        # runs them -- one at a time would stagger the registrations by seconds
        # and make this path subtly not the one the operator tested by hand.
        hook = (lambda line: self._scrape(line, step)) if watch else None
        sem = asyncio.Semaphore(settings.fanout_concurrency)

        async def one(device: Device) -> cmds.ExecJob:
            async with sem:
                return await cmds.start_job(pool, device, full, timeout=None, on_line=hook)

        started = list(await asyncio.gather(*(one(d) for d in target.devices)))
        self._jobs.extend(started)
        await asyncio.gather(*(cmds.wait_or_detach(j, LAUNCH_SETTLE_S) for j in started))

        failed = [j for j in started if j.result is not None and not j.result.ok]
        if failed:
            for j in failed:
                reason = j.result.error or f"exit {j.result.exit}"
                self._log(f"  ✗ {j.device_name}: {reason}", "stderr")
        alive = sum(1 for j in started if j.running)
        self._log(f"  {target.label}: {alive}/{len(started)} running", "stdout")
        return started[0] if started else None

    async def _await_server(
        self, job: cmds.ExecJob | None, project: Project, step: Step
    ) -> None:
        """Wait for `server.py` to exit — the project's completion signal.

        Polls rather than plain-awaiting so the board keeps moving: the elapsed
        clock, the phase, and whatever the server's output has revealed about
        its batch counter are all re-published while the wait goes on.
        """
        if job is None or job.task is None:
            await self._fail(step, "server job was never started")
            return

        deadline = time.time() + self._budget_s
        while not job.task.done():
            if self._stopping:
                break
            remaining = deadline - time.time()
            if remaining <= 0:
                # Reported, never killed: `server.py` writes the results archive
                # on the way out, so a budget that killed it would destroy the
                # very thing the run was for.
                self._log(
                    f"  ! budget {self._budget_s}s exhausted — leaving {project.name} alone",
                    "stderr",
                )
                step.detail = "budget exhausted; left running"
                await self._close(step, "failed", rc=2)
                return
            # `wait`, not `wait_for`: a timeout here means "poll again", and
            # `wait_for` would cancel the very job we are waiting on. Same
            # idiom as `wait_or_detach`, for the same reason.
            await asyncio.wait({job.task}, timeout=min(POLL_S, remaining))
            # Counters are written by the output hook as lines arrive; this only
            # republishes them, so the board advances between server prints too.
            self._emit_progress(step)

        if self._stopping:
            step.status = "stopped"
            self._emit_step(step)
            return

        result = job.result
        rc = (result.exit if result and not result.error else 1) if result else 1
        self._log(
            f"  server exited after {int(step.duration_s)}s (exit {rc})",
            "stdout" if rc == 0 else "stderr",
        )

        # The clients normally follow the server out. Give them the window to,
        # then say what is left rather than reaching for a signal.
        await asyncio.sleep(DRAIN_GRACE_S)
        stragglers = [j for j in self._jobs if j.running and j is not job]
        if stragglers:
            self._log(
                f"  {len(stragglers)} client(s) still running; "
                + ("they are cleaned up before the next project" if self._cleanup
                   else "cleanup is off, so they are left alone"),
                "stdout",
            )

        step.progress["phase"] = step.progress.get("phases", "3")
        await self._close(step, "ok" if rc == 0 else "failed", rc=rc)

    async def _cleanup_fleet(self, step: Step) -> None:
        """`pkill` the previous project's processes everywhere.

        Internal, so it is built here and not validated: the allow-list guards
        *operator input arriving through the API*, and this command needs the
        `;` and quoting that guard exists to reject. Same exemption the
        orchestrator's own `nohup … &` calls rely on.
        """
        step.progress_text = "cleaning up stragglers"
        self._emit_progress(step)
        every = [d for t in self._targets for d in t.devices]
        results = await cmds.fan_out(
            pool, every, CLEANUP_COMMAND, timeout=90, stream=False
        )
        left = 0
        for r in results:
            for line in (r.stdout or "").splitlines():
                if line.startswith("left="):
                    with contextlib.suppress(ValueError):
                        left += int(line.split("=", 1)[1].strip())
        unreachable = [r.device_name for r in results if r.error]
        note = f"  cleanup: {len(results) - len(unreachable)} host(s) swept"
        if left:
            note += f", {left} process(es) still up"
        if unreachable:
            note += f", unreachable: {', '.join(unreachable[:4])}"
        self._log(note, "stdout")

    # -------------------------------------------------------------- scraping
    @staticmethod
    def _scrape(line: str, step: Step) -> None:
        """Lift counters out of one line of the server's output, as it arrives.

        Best-effort by design, and called from inside the SSH read loop, so it
        stays a handful of regexes over one line rather than a parser over a
        buffer. These runs already print their batch counter and FPS, so reading
        them costs nothing; a project that prints neither falls back to the
        phase and elapsed bars, which are always true.

        Each pattern only ever *overwrites* its own key, so a line mentioning
        only FPS does not blank the batch counter the previous line set.
        """
        if not line:
            return
        if (m := _FPS_RE.search(line)) is not None:
            step.progress["fps"] = m.group(1)
        if (m := _BATCH_RE.search(line)) is not None:
            step.progress["batch"] = m.group(1)
            if m.group(2):
                step.progress["total"] = m.group(2)
        if (m := _REG_RE.search(line)) is not None:
            step.progress["reg"] = m.group(1) + (f"/{m.group(2)}" if m.group(2) else "")
        # A denominator is only worth taking once: these logs announce the frame
        # count at startup, then go on to print per-frame lines that would each
        # look like a new "total" and make the bar jump backwards.
        if "total" not in step.progress and (m := _TOTAL_RE.search(line)) is not None:
            step.progress["total"] = m.group(1)

    # --------------------------------------------------------------- plumbing
    def _emit_step(self, step: Step) -> None:
        run = self._run
        if run is None:
            return
        bus.event("autorun_step", run_id=run.id, step=step.to_dict())

    def _emit_progress(self, step: Step) -> None:
        run = self._run
        if run is None:
            return
        project = self._projects[step.index - 1] if step.index <= len(self._projects) else None
        if project and project.expected_s:
            step.progress["expected_s"] = str(project.expected_s)
        step.progress["elapsed_s"] = str(int(step.duration_s))
        bus.event(
            "autorun_progress",
            run_id=run.id,
            step=step.index,
            step_name=step.name,
            progress=dict(step.progress),
            text=step.progress_text,
        )

    def _log(self, text: str, stream: str = "stdout") -> None:
        bus.exec_line("", text, stream)

    async def _fail(self, step: Step, detail: str) -> None:
        step.detail = detail
        self._log(f"  ✗ {detail}", "stderr")
        await self._close(step, "failed", rc=1)

    async def _close(self, step: Step, status: str, *, rc: int) -> None:
        step.status = status
        step.rc = rc
        step.finished_at = time.time()
        self._emit_step(step)
        bus.exec_line(
            "", f"└─ {step.name}: {status} (rc={rc}) in {int(step.duration_s)}s",
            "stdout" if status == "ok" else "stderr",
        )
        # A failure is notified whatever `notify_steps` says: that flag exists to
        # silence the routine per-project "done", not the one message the whole
        # feature is for.
        if self._notify and status != "ok":
            await notifier.send(
                f"❌ <b>{esc(step.name)}</b> {status} (rc={rc})\n"
                f"<code>{esc(step.detail or step.name)}</code>"
            )

    async def _finish(self) -> None:
        run = self._run
        if run is None:
            return
        for step in run.steps:
            if step.status in ("running", "queued"):
                step.status = "stopped"
                step.finished_at = step.finished_at or time.time()

        counts = run.counts()
        run.status = (
            "stopped" if self._stopping
            else "ok" if counts["failed"] == 0 and counts["stopped"] == 0 and counts["ok"]
            else "failed"
        )
        run.finished_at = time.time()
        run.exit_code = 0 if run.status == "ok" else 1
        bus.exec_line(
            "",
            f"└─ queue {run.status} — {counts['ok']}/{counts['total']} ok",
            "stdout" if run.status == "ok" else "stderr",
        )
        bus.event("autorun_finished", run=run.to_dict())

        if self._notify:
            icon = {"ok": "✅", "failed": "❌", "stopped": "⏹"}.get(run.status, "•")
            lines = [
                f"{icon} <b>Project queue {run.status}</b>",
                f"{counts['ok']}/{counts['total']} ok · {int(run.duration_s // 60)}m",
            ]
            for s in run.steps:
                mark = {"ok": "✔", "failed": "✗", "stopped": "■"}.get(s.status, "·")
                lines.append(f"{mark} {esc(s.name)} — {int(s.duration_s)}s")
            await notifier.send("\n".join(lines))

    # ------------------------------------------------------------------- stop
    async def stop(self) -> dict[str, Any]:
        """Ctrl-C every job this queue started, and cancel the rest of the plan.

        Escalates SIGINT → SIGTERM → channel close per job (`JobRegistry`), so a
        project that writes its results on the way out still gets to.
        """
        run = self.active
        if run is None:
            return {"stopped": False, "note": "nothing was running"}

        self._stopping = True
        live = [j for j in self._jobs if j.running]
        bus.exec_line("", f"^C stopping the queue ({len(live)} job(s))", "meta")
        outcomes = await asyncio.gather(
            *(cmds.jobs.interrupt(j) for j in live), return_exceptions=True
        )
        if self._task is not None and not self._task.done():
            # The driver notices `_stopping` at its next poll; give it that long
            # before taking the task away, so `_finish` still runs.
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(self._task), timeout=POLL_S + 5)
        return {
            "stopped": True,
            "jobs": len(live),
            "outcomes": [str(o) for o in outcomes],
            "run": self._run.to_dict() if self._run else None,
        }


# ------------------------------------------------------------------ resolving
def _norm(label: str) -> str:
    """Fold a preset label the way the UI does, so "run  stage 1" and
    "Run stage 1" are the same handle (see live-patch.js `normLabel`)."""
    return re.sub(r"\s+", " ", (label or "").strip().lower())


def _preset(presets: dict[str, str], labels: list[str]) -> str:
    for label in labels:
        if command := presets.get(_norm(label)):
            return command
    return ""


def _stage_targets(devices: list[Device], presets: dict[str, str]) -> list[Target]:
    """One target per stage, in stage order, with the command saved for it.

    The stage's own name is tried before its position, exactly as the Control
    tab's **select all** does -- a stage renamed to "Edge" keeps finding
    `run Edge`, and the stock names still resolve through `run stage <n>`.
    """
    order: list[str] = []
    by_stage: dict[str, list[Device]] = {}
    for d in devices:
        key = d.stage_id or d.stage_name or f"stage-{d.cluster_id}"
        if key not in by_stage:
            by_stage[key] = []
            order.append(key)
        by_stage[key].append(d)

    out: list[Target] = []
    for i, key in enumerate(order, start=1):
        members = by_stage[key]
        label = members[0].stage_name or f"Stage {i}"
        out.append(
            Target(
                key=key,
                label=label,
                devices=members,
                command=_preset(presets, [f"run {label}", f"run stage {i}"]),
            )
        )
    return out


#: Process-wide singleton, like the schedule runner it shares a lock with.
runner = ProjectQueueRunner()
