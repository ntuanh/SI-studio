"""Deploy shards, run split inference, aggregate live metrics (guide §6).

Flow
----
deploy(cluster)  scp head.pt -> edges, tail.pt -> clouds, plus the agent code
                 and bootstrap.sh; then launch edge_agent / cloud_agent.
start(cluster)   declare queues, begin consuming metrics_queue, start the
                 broadcast loop that emits §6 payloads at METRICS_BROADCAST_HZ.
stop(cluster)    stop the agents, cancel consumers, drain the queues.

The agents report per-frame timings to `metrics_queue`; this module keeps a
rolling window per cluster and derives utilization / fps / queue depth from it.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlmodel.ext.asyncio.session import AsyncSession

from ..config import settings
from ..models import Cluster, Device, Run
from ..services import topology
from ..services.metrics_bus import bus
from ..ssh import commands as cmds
from ..ssh.pool import SSHError, pool
from . import simulation as sim
from .broker import FPS_QUEUE, METRICS_QUEUE, broker, intermediate_queue

log = logging.getLogger(__name__)

AGENT_DIR = Path(__file__).resolve().parent.parent.parent / "agent"


# --------------------------------------------------------------------- windows
@dataclass
class _Window:
    """Rolling per-frame samples for one cluster."""

    edge_ms: deque[float] = field(default_factory=lambda: deque(maxlen=settings.metrics_window))
    transfer_ms: deque[float] = field(default_factory=lambda: deque(maxlen=settings.metrics_window))
    cloud_ms: deque[float] = field(default_factory=lambda: deque(maxlen=settings.metrics_window))
    e2e_ms: deque[float] = field(default_factory=lambda: deque(maxlen=settings.metrics_window))
    msg_mb: deque[float] = field(default_factory=lambda: deque(maxlen=settings.metrics_window))
    #: (monotonic_ts) of each completed frame -- fps comes from this.
    completions: deque[float] = field(default_factory=lambda: deque(maxlen=600))
    #: device_id -> rolling stage time on that device
    per_device: dict[str, deque[float]] = field(default_factory=dict)
    cut: int | None = None
    frames: int = 0
    last_report: float = 0.0

    def device_window(self, device_id: str) -> deque[float]:
        if device_id not in self.per_device:
            self.per_device[device_id] = deque(maxlen=settings.metrics_window)
        return self.per_device[device_id]

    def fps(self) -> float:
        """Frames completed per second over the last ~2s of wall clock."""
        if len(self.completions) < 2:
            return 0.0
        now = self.completions[-1]
        cutoff = now - 2.0
        recent = [t for t in self.completions if t >= cutoff]
        if len(recent) < 2:
            recent = list(self.completions)[-2:]
        span = recent[-1] - recent[0]
        if span <= 0:
            return 0.0
        return (len(recent) - 1) / span


def _mean(values: deque[float] | list[float]) -> float:
    return (sum(values) / len(values)) if values else 0.0


# ---------------------------------------------------------------- run tracking
@dataclass
class ActiveRun:
    run_id: str
    cluster_id: int
    queue_name: str
    cut: int
    model_name: str
    num_bit: int
    batch_size: int
    edge_ids: list[str]
    cloud_ids: list[str]
    started_at: float
    status: str = "running"


class Orchestrator:
    def __init__(self) -> None:
        self._runs: dict[int, ActiveRun] = {}
        self._windows: dict[int, _Window] = {}
        self._broadcast_task: asyncio.Task[None] | None = None
        self._consuming = False
        self._lock = asyncio.Lock()

    # ================================================================ deploy
    async def deploy(
        self,
        session: AsyncSession,
        cluster_id: int,
        *,
        install_deps: bool = False,
        head_shard: str | None = None,
        tail_shard: str | None = None,
    ) -> dict[str, Any]:
        """Push shards + agent code to every device in the cluster (§6.1)."""
        cl_row, edges, clouds = await self._resolve(session, cluster_id)
        if not edges or not clouds:
            raise ValueError(
                f"cluster {cluster_id} is idle: needs at least one edge and one cloud device"
            )

        head = self._shard_path(head_shard or "head.pt")
        tail = self._shard_path(tail_shard or "tail.pt")
        missing = [p.name for p in (head, tail) if not p.is_file()]
        if missing:
            raise FileNotFoundError(
                f"missing shard(s) {missing} in {settings.shards_path}. "
                "Build them with `python tools/split_model.py --cut N`."
            )

        root = settings.remote_root
        bus.event("deploy_started", cluster=cluster_id, devices=len(edges) + len(clouds))

        transfers: list[dict[str, Any]] = []

        # --- agent code + bootstrap to every device ---
        all_devices = edges + clouds
        for name in ("edge_agent.py", "cloud_agent.py", "codec.py", "bootstrap.sh"):
            local = AGENT_DIR / name
            if not local.is_file():
                continue
            results = await cmds.scp_put_many(
                pool, all_devices, local, f"{root}/agent/{name}", verify=False
            )
            transfers += [r.to_dict() for r in results]

        # --- shards to the side that needs them ---
        head_res = await cmds.scp_put_many(pool, edges, head, f"{root}/models/head.pt")
        tail_res = await cmds.scp_put_many(pool, clouds, tail, f"{root}/models/tail.pt")
        transfers += [r.to_dict() for r in head_res + tail_res]

        failed = [t for t in transfers if not t["ok"]]

        # --- optional dependency bootstrap ---
        bootstrap: list[dict[str, Any]] = []
        if install_deps:
            bus.event("bootstrap_started", cluster=cluster_id)
            res = await cmds.fan_out(
                pool, all_devices, f"bash {root}/agent/bootstrap.sh", timeout=900
            )
            bootstrap = [r.to_dict() for r in res]

        bus.event(
            "deploy_finished",
            cluster=cluster_id,
            transfers=len(transfers),
            failed=len(failed),
        )
        return {
            "cluster": cluster_id,
            "queue": cl_row.ensure_queue_name(),
            "edge_devices": [d.id for d in edges],
            "cloud_devices": [d.id for d in clouds],
            "transfers": transfers,
            "bootstrap": bootstrap,
            "ok": not failed,
        }

    # ================================================================= start
    async def start(
        self,
        session: AsyncSession,
        cluster_id: int,
        *,
        source: str = "",
        max_frames: int | None = None,
    ) -> dict[str, Any]:
        """Declare queues, launch the agents, begin collecting (§6.2-6.4)."""
        async with self._lock:
            if cluster_id in self._runs:
                raise ValueError(f"cluster {cluster_id} already has a run in flight")

            cl_row, edges, clouds = await self._resolve(session, cluster_id)
            if not edges or not clouds:
                raise ValueError(
                    f"cluster {cluster_id} is idle: needs at least one edge and one cloud device"
                )

            # The cut comes from the simulator so live and simulated runs agree.
            plan = await topology.simulate_cluster(session, cluster_id)
            if plan is None:
                raise ValueError(f"cluster {cluster_id} could not be planned")
            cut = plan.cut

            queue_name = cl_row.ensure_queue_name()
            await broker.declare_cluster_queues(cluster_id)
            await self._ensure_collector()

            run = Run(
                id=uuid.uuid4().hex[:12],
                cluster_id=cluster_id,
                status="running",
                model_name=cl_row.model_name,
                cut_layer=cut,
                num_bit=cl_row.num_bit,
                batch_size=cl_row.batch_size,
            )

            # --- launch agents ---
            launches: list[dict[str, Any]] = []
            pids: dict[str, Any] = {}
            for d in clouds:  # consumers first, so nothing queues up unread
                res = await self._launch(d, "cloud", cl_row, cut, queue_name, run.id, None)
                launches.append(res)
                pids[d.id] = res.get("pid")
            for d in edges:
                res = await self._launch(d, "edge", cl_row, cut, queue_name, run.id, max_frames)
                launches.append(res)
                pids[d.id] = res.get("pid")

            failed = [l for l in launches if not l["ok"]]
            if failed:
                # Roll back so a half-started cluster can't linger.
                await self._kill_agents(edges + clouds)
                run.status = "error"
                run.detail = "; ".join(f"{l['device_id']}: {l['error']}" for l in failed)[:500]
                run.stopped_at = datetime.now(timezone.utc)
                run.device_pids = pids
                session.add(run)
                await session.commit()
                raise RuntimeError(f"agent launch failed -- {run.detail}")

            run.device_pids = pids
            session.add(run)
            await session.commit()

            self._windows[cluster_id] = _Window(cut=cut)
            self._runs[cluster_id] = ActiveRun(
                run_id=run.id,
                cluster_id=cluster_id,
                queue_name=queue_name,
                cut=cut,
                model_name=cl_row.model_name,
                num_bit=cl_row.num_bit,
                batch_size=cl_row.batch_size,
                edge_ids=[d.id for d in edges],
                cloud_ids=[d.id for d in clouds],
                started_at=time.monotonic(),
            )
            self._ensure_broadcaster()

            bus.event(
                "run_started",
                cluster=cluster_id, run_id=run.id, cut=cut,
                model=cl_row.model_name, queue=queue_name, source=source or "api",
            )
            return {
                "run_id": run.id,
                "cluster": cluster_id,
                "cut": cut,
                "layer_count": plan.layer_count,
                "queue": queue_name,
                "model_name": cl_row.model_name,
                "num_bit": cl_row.num_bit,
                "batch_size": cl_row.batch_size,
                "launches": launches,
                "predicted": plan.to_payload(source="sim"),
            }

    # ================================================================== stop
    async def stop(
        self, session: AsyncSession, cluster_id: int, *, drain: bool = True
    ) -> dict[str, Any]:
        """Stop the agents, cancel collection, drain the queues (§7 /run/stop)."""
        async with self._lock:
            active = self._runs.pop(cluster_id, None)
            _, edges, clouds = await self._resolve(session, cluster_id)

            kills = await self._kill_agents(edges + clouds)

            purged = {}
            if drain:
                qname = active.queue_name if active else intermediate_queue(cluster_id)
                purged[qname] = await broker.purge(qname)

            if active is not None:
                run = await session.get(Run, active.run_id)
                if run is not None:
                    run.status = "stopped"
                    run.stopped_at = datetime.now(timezone.utc)
                    session.add(run)
                    await session.commit()

            self._windows.pop(cluster_id, None)
            bus.clear_metrics(cluster_id)

            if not self._runs:
                await self._teardown_collector()

            bus.event("run_stopped", cluster=cluster_id, run_id=active.run_id if active else None)
            return {
                "cluster": cluster_id,
                "run_id": active.run_id if active else None,
                "stopped": [k.to_dict() for k in kills],
                "purged": purged,
            }

    async def stop_all(self, session: AsyncSession) -> list[dict[str, Any]]:
        return [await self.stop(session, cid) for cid in list(self._runs)]

    # ============================================================== collection
    async def _ensure_collector(self) -> None:
        if self._consuming:
            return
        await broker.connect()
        await broker.consume(METRICS_QUEUE, self._on_metric)
        self._consuming = True

    async def _teardown_collector(self) -> None:
        if not self._consuming:
            return
        await broker.cancel_consumer(METRICS_QUEUE)
        self._consuming = False
        if self._broadcast_task is not None:
            self._broadcast_task.cancel()
            self._broadcast_task = None

    async def _on_metric(self, data: dict[str, Any]) -> None:
        """Handle one per-frame report from a cloud agent."""
        cid = data.get("cluster")
        if cid is None:
            return
        cid = int(cid)
        win = self._windows.get(cid)
        if win is None:  # a late report from a stopped run
            return

        now = time.monotonic()
        edge_ms = _f(data.get("edge_ms"))
        transfer_ms = _f(data.get("transfer_ms"))
        cloud_ms = _f(data.get("cloud_ms"))
        e2e = data.get("e2e_ms")
        e2e_ms = _f(e2e) if e2e is not None else (edge_ms + transfer_ms + cloud_ms)

        win.edge_ms.append(edge_ms)
        win.transfer_ms.append(transfer_ms)
        win.cloud_ms.append(cloud_ms)
        win.e2e_ms.append(e2e_ms)
        win.msg_mb.append(_f(data.get("msg_mb")))
        win.completions.append(now)
        win.frames += 1
        win.last_report = now
        if data.get("cut") is not None:
            win.cut = int(data["cut"])

        if (eid := data.get("edge_device_id")) and edge_ms:
            win.device_window(str(eid)).append(edge_ms)
        if (cdid := data.get("device_id")) and cloud_ms:
            win.device_window(str(cdid)).append(cloud_ms)

    # ------------------------------------------------------------- broadcasting
    def _ensure_broadcaster(self) -> None:
        if self._broadcast_task is None or self._broadcast_task.done():
            self._broadcast_task = asyncio.create_task(self._broadcast_loop())

    async def _broadcast_loop(self) -> None:
        period = 1.0 / max(0.1, settings.metrics_broadcast_hz)
        try:
            while self._runs:
                for cid in list(self._runs):
                    try:
                        payload = await self.live_payload(cid)
                    except Exception:
                        log.exception("failed to build metrics for cluster %s", cid)
                        continue
                    if payload is not None:
                        bus.metrics(payload)
                await asyncio.sleep(period)
        except asyncio.CancelledError:
            raise
        finally:
            self._broadcast_task = None

    async def live_payload(self, cluster_id: int) -> dict[str, Any] | None:
        """Build the §6 metric payload from the rolling window."""
        active = self._runs.get(cluster_id)
        win = self._windows.get(cluster_id)
        if active is None or win is None:
            return None

        edge_ms = _mean(win.edge_ms)
        transfer_ms = _mean(win.transfer_ms)
        cloud_ms = _mean(win.cloud_ms)
        bottleneck = max(edge_ms, transfer_ms, cloud_ms) or 1.0

        stats = await broker.stats(active.queue_name)
        fps = win.fps()

        devices: list[dict[str, Any]] = []
        for dev_id in active.edge_ids + active.cloud_ids:
            samples = win.per_device.get(dev_id)
            util = min(1.0, _mean(samples) / bottleneck) if samples else 0.0
            devices.append(
                {
                    "id": dev_id,
                    "util": round(util, 4),
                    "role": "tail" if dev_id in active.cloud_ids else "head",
                }
            )

        stale = win.last_report > 0 and (time.monotonic() - win.last_report) > 5.0
        return {
            "cluster": cluster_id,
            "cut": win.cut if win.cut is not None else active.cut,
            "edge_ms": round(edge_ms, 3),
            "transfer_ms": round(transfer_ms, 3),
            "cloud_ms": round(cloud_ms, 3),
            "e2e_ms": round(_mean(win.e2e_ms), 3),
            "msg_mb": round(_mean(win.msg_mb), 4),
            "fps": round(fps, 3),
            "edge_util": round(edge_ms / bottleneck, 4),
            "transfer_util": round(transfer_ms / bottleneck, 4),
            "cloud_util": round(cloud_ms / bottleneck, 4),
            "queue_depth": int(stats.get("queue_depth", 0)),
            "devices": devices,
            # extras
            "run_id": active.run_id,
            "queue": active.queue_name,
            "model_name": active.model_name,
            "num_bit": active.num_bit,
            "frames": win.frames,
            "uptime_s": round(time.monotonic() - active.started_at, 1),
            "consumers": stats.get("consumers"),
            "stale": stale,
            "source": "live",
        }

    # ================================================================ helpers
    def is_live(self, cluster_id: int) -> bool:
        return cluster_id in self._runs

    def active_runs(self) -> list[dict[str, Any]]:
        return [
            {
                "run_id": r.run_id,
                "cluster": r.cluster_id,
                "cut": r.cut,
                "queue": r.queue_name,
                "model_name": r.model_name,
                "status": r.status,
                "frames": (self._windows.get(cid).frames if self._windows.get(cid) else 0),
                "uptime_s": round(time.monotonic() - r.started_at, 1),
            }
            for cid, r in self._runs.items()
        ]

    async def _resolve(
        self, session: AsyncSession, cluster_id: int
    ) -> tuple[Cluster, list[Device], list[Device]]:
        cfg = await topology.load_global_config(session)
        cl_row = await topology.ensure_cluster(session, cluster_id, model_name=cfg.model_name)
        edges, clouds = await topology.cluster_devices(session, cluster_id)
        return cl_row, edges, clouds

    def _shard_path(self, name: str) -> Path:
        """Resolve a shard name inside SHARDS_DIR, refusing path escapes."""
        candidate = (settings.shards_path / name).resolve()
        if settings.shards_path.resolve() not in candidate.parents:
            raise ValueError(f"shard path escapes shards dir: {name!r}")
        return candidate

    # ---------------------------------------------------------- agent lifecycle
    async def _launch(
        self,
        device: Device,
        role: str,
        cl: Cluster,
        cut: int,
        queue_name: str,
        run_id: str,
        max_frames: int | None,
    ) -> dict[str, Any]:
        """Start one agent under nohup and capture its PID."""
        root = settings.remote_root
        py = settings.remote_python
        q = shlex.quote

        argv = [
            py, f"{root}/agent/{role}_agent.py",
            "--broker-url", q(settings.agent_broker_url),
            "--queue", q(queue_name),
            "--cluster", str(cl.id),
            "--device-id", q(device.id),
            "--run-id", q(run_id),
        ]
        if role == "edge":
            argv += [
                "--model", f"{root}/models/head.pt",
                "--cut", str(cut),
                "--num-bit", str(cl.num_bit),
                "--batch", str(cl.batch_size),
            ]
            if max_frames:
                argv += ["--max-frames", str(max_frames)]
        else:
            argv += ["--model", f"{root}/models/tail.pt", "--metrics-queue", METRICS_QUEUE,
                     "--fps-queue", FPS_QUEUE]

        logfile = f"{root}/logs/{role}_agent_{cl.id}.log"
        pidfile = f"{root}/run/{role}_agent_{cl.id}.pid"
        # Trusted, internally constructed command -- deliberately not passed
        # through validate_command(), which would reject the redirects and `&`.
        #
        # `mkdir` must be its own statement: in `mkdir ... && nohup ... &` the
        # `&` backgrounds the whole AND-list, so `$!` would be the subshell's
        # PID rather than the agent's. With the two separated, `nohup` execs
        # python in place and `$!` is the agent process itself.
        launch = "\n".join(
            [
                f"mkdir -p {root}/logs {root}/run || exit 1",
                f"nohup {' '.join(argv)} >> {logfile} 2>&1 &",
                f"echo $! > {pidfile}",
                f"cat {pidfile}",
            ]
        )

        try:
            conn = await pool.get(device)
        except SSHError as exc:
            bus.exec_line(device.id, f"✗ {device.name}: {exc}", "stderr")
            return {"device_id": device.id, "role": role, "ok": False, "error": str(exc), "pid": None}

        res = await cmds.run_command(conn, f"sh -lc {shlex.quote(launch)}", timeout=60)
        pid = (res.stdout or "").strip().splitlines()[-1].strip() if res.stdout.strip() else ""

        if res.error or res.exit != 0 or not pid.isdigit():
            error = res.error or (res.stderr.strip() or f"exit {res.exit}")
            bus.exec_line(device.id, f"✗ {role}_agent failed to start: {error}", "stderr")
            return {"device_id": device.id, "role": role, "ok": False, "error": error, "pid": None}

        # Confirm it survived import time rather than trusting the PID alone.
        await asyncio.sleep(1.0)
        alive = await cmds.run_command(conn, f"kill -0 {pid} 2>/dev/null; echo $?", timeout=20)
        if (alive.stdout or "").strip().splitlines()[-1].strip() != "0":
            tail = await cmds.run_command(conn, f"tail -n 20 {logfile}", timeout=20)
            error = f"agent exited immediately; log tail: {(tail.stdout or '').strip()[-400:]}"
            bus.exec_line(device.id, f"✗ {role}_agent: {error}", "stderr")
            return {"device_id": device.id, "role": role, "ok": False, "error": error, "pid": None}

        bus.exec_line(device.id, f"✓ {role}_agent started (pid {pid})", "stdout")
        return {"device_id": device.id, "role": role, "ok": True, "error": "", "pid": int(pid)}

    async def _kill_agents(self, devices: list[Device]) -> list[cmds.CmdResult]:
        if not devices:
            return []
        root = settings.remote_root
        kill = (
            f"pkill -f '{root}/agent/edge_agent.py' ; "
            f"pkill -f '{root}/agent/cloud_agent.py' ; "
            "exit 0"
        )
        results = []
        for d in devices:
            try:
                conn = await pool.get(d)
            except SSHError as exc:
                results.append(
                    cmds.CmdResult(device_id=d.id, device_name=d.name, command=kill, error=str(exc))
                )
                continue
            r = await cmds.run_command(conn, f"sh -lc {shlex.quote(kill)}", timeout=30)
            r.device_id, r.device_name = d.id, d.name
            results.append(r)
            bus.exec_line(d.id, "⏹ agents stopped", "stdout")
        return results


def _f(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


#: Process-wide singleton.
orchestrator = Orchestrator()
