"""Device inventory + spec probing (guide §7)."""

from __future__ import annotations

import logging
import re
import shlex
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Body, Depends, HTTPException, status
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from ..auth import require_token
from ..config import settings
from ..db import get_session
from ..models import Device
from ..schemas import (
    DeviceIn,
    DeviceOut,
    DevicePatch,
    FleetMeasureOut,
    MeasureOut,
    ProbeOut,
)
from ..services import measure, topology
from ..services.measure import GPU_PEAK_GFLOPS, last_json as _last_json
from ..ssh import commands as cmds
from ..ssh import secrets_store
from ..ssh.pool import SSHError, pool

log = logging.getLogger(__name__)

router = APIRouter(prefix="/devices", tags=["devices"], dependencies=[Depends(require_token)])


# ------------------------------------------------------------------ serializer
def to_out(d: Device) -> DeviceOut:
    return DeviceOut(
        id=d.id,
        name=d.name,
        kind=d.kind,
        cluster_id=d.cluster_id,
        host=d.host,
        port=d.port,
        username=d.username,
        auth_method=d.auth_method,
        key_ref=d.key_ref,
        gflops=d.gflops,
        bandwidth_mb_s=d.bandwidth_mb_s,
        latency_ms=d.latency_ms,
        stage_id=d.stage_id,
        stage_name=d.stage_name,
        role=d.resolved_role,
        side=d.side,
        bw=d.bandwidth_mb_s,
        lat=d.latency_ms,
        cluster=d.cluster_id,
        has_password=secrets_store.has_password(d.id),
        ssh_status=pool.status(d.id),
        probed_at=d.probed_at,
        probe_info=d.probe_info or {},
    )


async def _get_or_404(session: AsyncSession, device_id: str) -> Device:
    d = await session.get(Device, device_id)
    if d is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"device {device_id!r} not found")
    return d


# ------------------------------------------------------------------- endpoints
@router.get("", response_model=list[DeviceOut])
async def list_devices(session: AsyncSession = Depends(get_session)) -> list[DeviceOut]:
    rows = (await session.exec(select(Device))).all()
    return [to_out(d) for d in sorted(rows, key=lambda x: (x.cluster_id, x.side, x.name))]


@router.post("", response_model=DeviceOut, status_code=status.HTTP_201_CREATED)
async def create_device(
    payload: DeviceIn, session: AsyncSession = Depends(get_session)
) -> DeviceOut:
    device_id = payload.id or f"d{uuid.uuid4().hex[:5]}"
    if await session.get(Device, device_id) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"device {device_id!r} already exists")

    if payload.auth_method == "key" and payload.key_ref:
        _require_key(payload.key_ref)

    d = Device(
        id=device_id,
        name=payload.name,
        kind=payload.kind,
        cluster_id=payload.cluster_id,
        host=payload.host,
        port=payload.port,
        username=payload.username,
        auth_method=payload.auth_method,
        key_ref=payload.key_ref,
        gflops=payload.gflops,
        bandwidth_mb_s=payload.bandwidth_mb_s,
        latency_ms=payload.latency_ms,
        stage_id=payload.stage_id,
        stage_name=payload.stage_name,
        role=payload.role,
    )
    session.add(d)
    await session.commit()
    await session.refresh(d)

    if payload.password:
        secrets_store.set_password(d.id, payload.password)

    await topology.ensure_cluster(
        session, d.cluster_id, model_name=(await topology.load_global_config(session)).model_name
    )
    return to_out(d)


@router.patch("/{device_id}", response_model=DeviceOut)
async def update_device(
    device_id: str, payload: DevicePatch, session: AsyncSession = Depends(get_session)
) -> DeviceOut:
    d = await _get_or_404(session, device_id)
    data = payload.model_dump(exclude_unset=True, exclude_none=True)

    password = data.pop("password", None)
    if "key_ref" in data and data["key_ref"]:
        _require_key(data["key_ref"])

    # Changing where/who we connect as invalidates the open session.
    reconnect_fields = {"host", "port", "username", "auth_method", "key_ref"}
    needs_reset = any(f in data and getattr(d, f) != data[f] for f in reconnect_fields)

    for field, value in data.items():
        setattr(d, field, value)
    session.add(d)
    await session.commit()
    await session.refresh(d)

    if password is not None:
        secrets_store.set_password(d.id, password)
    if needs_reset or password is not None:
        await pool.disconnect(d.id)

    return to_out(d)


# `response_model=None`: postponed annotations make `-> None` resolve to
# NoneType, which FastAPI would treat as a response body on a 204.
@router.delete("/{device_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_device(device_id: str, session: AsyncSession = Depends(get_session)) -> None:
    d = await _get_or_404(session, device_id)
    await pool.disconnect(device_id)
    secrets_store.forget_device(device_id)
    await session.delete(d)
    await session.commit()


# --------------------------------------------------------------------- measure
@router.post("/measure", response_model=FleetMeasureOut)
async def measure_fleet(
    session: AsyncSession = Depends(get_session),
    device_ids: list[str] | None = Body(default=None, embed=True),
    apply: bool = Body(default=True, embed=True),
    contention: bool = Body(default=True, embed=True),
    bandwidth_basis: str = Body(default="shared", embed=True),
    iperf_server: str | None = Body(default=None, embed=True),
    latency_target: str | None = Body(default=None, embed=True),
) -> FleetMeasureOut:
    """Measure every device's specs automatically, contention included.

    Compute and latency are measured on all devices at once; **bandwidth is
    not**, because the devices share an uplink and a broker. Measuring twenty
    machines together would have each of them report its share of one link, so
    the transfer test runs strictly one device at a time.

    That gives the solo figure. `contention=true` then adds a second pass with
    every device transferring simultaneously, which is what actually happens
    during a run -- the ratio between the two is reported per device, so
    machines on independent links (~1.0) separate themselves from machines
    fighting over one uplink without anyone having to describe the network.

    `bandwidth_basis` picks which figure is written to the spec field:
    `shared` (default, what a run really gets) or `solo` (the link's capacity).
    """
    if bandwidth_basis not in ("shared", "solo"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"bandwidth_basis must be 'shared' or 'solo', got {bandwidth_basis!r}",
        )

    rows = (await session.exec(select(Device))).all()
    devices = [d for d in rows if device_ids is None or d.id in set(device_ids)]
    if not devices:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no matching devices")

    results, summary = await measure.measure_fleet(
        pool,
        devices,
        contention=contention,
        iperf_server=iperf_server,
        latency_target=latency_target,
    )

    if apply:
        by_id = {d.id: d for d in devices}
        for m in results:
            if not m.ok:
                continue
            measure.apply_to_device(by_id[m.device_id], m, bandwidth_basis)
            session.add(by_id[m.device_id])
        await session.commit()

    return FleetMeasureOut(
        results=[MeasureOut(**m.to_dict()) for m in results],
        summary=summary,
        applied=apply,
        bandwidth_basis=bandwidth_basis,
    )


@router.post("/{device_id}/measure", response_model=MeasureOut)
async def measure_one(
    device_id: str,
    session: AsyncSession = Depends(get_session),
    apply: bool = Body(default=True, embed=True),
    bandwidth: bool = Body(default=True, embed=True),
    iperf_server: str | None = Body(default=None, embed=True),
    latency_target: str | None = Body(default=None, embed=True),
) -> MeasureOut:
    """Measure one device by the best method each spec admits.

    * `gflops` -- a 3x3 convolution benchmark auto-tuned to a fixed time
      budget, which is what a CNN stage actually costs; the matmul figure is
      kept in `info` as a cross-check.
    * `bandwidth_mb_s` -- a timed pull of an incompressible blob over the SSH
      connection, or `iperf3` when `iperf_server` is given.
    * `latency_ms` -- TCP handshake to the broker's AMQP port, minimum of
      several, so it survives networks that drop ICMP.

    The bandwidth phase takes the same fleet-wide lock `POST /devices/measure`
    uses, so calling this while a fleet pass is running waits its turn rather
    than corrupting both numbers. This route only ever reports the *solo*
    figure -- the contended one needs the whole fleet, so ask for it there.
    """
    d = await _get_or_404(session, device_id)
    m = await measure.measure_device(
        pool,
        d,
        bandwidth=bandwidth,
        iperf_server=iperf_server,
        latency_target=latency_target,
    )
    if apply and m.ok:
        measure.apply_to_device(d, m, "solo")
        session.add(d)
        await session.commit()
    return MeasureOut(**m.to_dict())


# ----------------------------------------------------------------------- probe
#: Ships to the device as `python3 -c <source>`; prints one JSON line.
_BENCH_SRC = r"""
import json, time
out = {"ok": False}
try:
    import torch
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    n = 2048 if dev == "cpu" else 4096
    a = torch.randn(n, n, device=dev)
    b = torch.randn(n, n, device=dev)
    for _ in range(2):
        c = a @ b
    if dev == "cuda":
        torch.cuda.synchronize()
    iters = 5 if dev == "cpu" else 20
    t0 = time.perf_counter()
    for _ in range(iters):
        c = a @ b
    if dev == "cuda":
        torch.cuda.synchronize()
    dt = time.perf_counter() - t0
    flops = 2.0 * n ** 3 * iters
    out = {"ok": True, "device": dev, "gflops": round(flops / dt / 1e9, 1),
           "torch": torch.__version__, "n": n, "iters": iters}
except Exception as exc:
    out = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
print(json.dumps(out))
"""


@router.post("/{device_id}/probe", response_model=ProbeOut)
async def probe_device(
    device_id: str,
    session: AsyncSession = Depends(get_session),
    benchmark: bool = Body(default=True, embed=True),
    iperf_server: str | None = Body(default=None, embed=True),
    latency_target: str | None = Body(default=None, embed=True),
    apply: bool = Body(default=True, embed=True),
) -> ProbeOut:
    """SSH in and fill `gflops` / `bandwidth_mb_s` / `latency_ms` from the box.

    * `gflops` -- measured with a torch matmul when torch is present, otherwise
      estimated from the GPU name (flagged in `warnings`).
    * `bandwidth_mb_s` -- `iperf3 -c <iperf_server>` when given, else the NIC's
      reported link speed (also an upper bound, so flagged).
    * `latency_ms` -- ICMP RTT to `latency_target` (defaults to the broker host),
      falling back to the SSH command round trip.
    """
    d = await _get_or_404(session, device_id)
    warnings: list[str] = []
    info: dict[str, Any] = {}

    try:
        conn = await pool.get(d)
    except SSHError as exc:
        return ProbeOut(device_id=device_id, ok=False, error=str(exc))

    # --- inventory ---
    for label, cmd in (
        ("hostname", "hostname"),
        ("uname", "uname -sr"),
        ("nproc", "nproc"),
        ("os", "cat /etc/os-release"),
    ):
        r = await cmds.run_command(conn, cmd, timeout=20)
        if r.ok:
            info[label] = r.stdout.strip()

    gpu = await cmds.run_command(
        conn,
        "nvidia-smi --query-gpu=name,memory.total,utilization.gpu --format=csv,noheader",
        timeout=30,
    )
    gpu_name = ""
    if gpu.ok and gpu.stdout.strip():
        info["nvidia_smi"] = gpu.stdout.strip()
        gpus = [line.strip() for line in gpu.stdout.strip().splitlines() if line.strip()]
        info["gpu_count"] = len(gpus)
        gpu_name = gpus[0].split(",")[0].strip()
        info["gpu_name"] = gpu_name
    else:
        warnings.append("nvidia-smi unavailable -- treating as a CPU-only device")

    # --- gflops ---
    gflops: float | None = None
    if benchmark:
        bench = await cmds.run_command(
            conn, f"{settings.remote_python} -c {shlex.quote(_BENCH_SRC)}", timeout=300
        )
        parsed = _last_json(bench.stdout)
        if parsed and parsed.get("ok"):
            gflops = float(parsed["gflops"])
            info["benchmark"] = parsed
        else:
            reason = (parsed or {}).get("error") or (bench.error or bench.stderr.strip()[:200])
            warnings.append(f"on-device benchmark failed ({reason or 'unknown'})")

    if gflops is None and gpu_name:
        key = next((k for k in GPU_PEAK_GFLOPS if k in gpu_name.lower()), None)
        if key:
            count = int(info.get("gpu_count", 1))
            gflops = GPU_PEAK_GFLOPS[key] * count
            warnings.append(
                f"gflops estimated from vendor peak FP32 for {gpu_name!r} x{count}; "
                "peak overstates sustained throughput -- prefer the benchmark"
            )

    # --- bandwidth ---
    bandwidth: float | None = None
    if iperf_server:
        target = shlex.quote(iperf_server)
        ip = await cmds.run_command(conn, f"iperf3 -c {target} -t 3 -J", timeout=90)
        payload = _last_json(ip.stdout)
        bits = (((payload or {}).get("end") or {}).get("sum_sent") or {}).get("bits_per_second")
        if bits:
            bandwidth = round(float(bits) / 8e6, 2)  # bits/s -> MB/s
            info["iperf3_mb_s"] = bandwidth
        else:
            warnings.append(f"iperf3 to {iperf_server} produced no result")

    if bandwidth is None:
        link = await cmds.run_command(
            conn,
            "cat /sys/class/net/*/speed 2>/dev/null | sort -rn | head -n1",
            timeout=20,
        )
        raw = (link.stdout or "").strip().splitlines()
        mbit = next((int(x) for x in raw if x.strip().lstrip("-").isdigit() and int(x) > 0), None)
        if mbit:
            bandwidth = round(mbit / 8.0, 2)  # Mbit/s -> MB/s
            info["nic_link_mbit"] = mbit
            warnings.append(
                "bandwidth taken from NIC link speed (theoretical); pass iperf_server "
                "for a measured figure"
            )

    # --- latency ---
    latency: float | None = None
    target = latency_target or _broker_host()
    if target:
        ping = await cmds.run_command(
            conn, f"ping -c 3 -w 5 {shlex.quote(target)}", timeout=30
        )
        m = re.search(r"=\s*[\d.]+/([\d.]+)/", ping.stdout or "")
        if m:
            latency = round(float(m.group(1)), 2)
            info["ping_target"] = target
    if latency is None:
        rtt = await cmds.run_command(conn, "true", timeout=20)
        latency = round(rtt.duration_ms, 2)
        warnings.append("latency measured as SSH command round trip, not ICMP to the broker")

    # --- persist ---
    if apply:
        if gflops is not None:
            d.gflops = gflops
        if bandwidth is not None:
            d.bandwidth_mb_s = bandwidth
        if latency is not None:
            d.latency_ms = latency
        d.probed_at = datetime.now(timezone.utc)
        d.probe_info = info
        session.add(d)
        await session.commit()

    return ProbeOut(
        device_id=device_id,
        ok=True,
        gflops=gflops,
        bandwidth_mb_s=bandwidth,
        latency_ms=latency,
        info=info,
        warnings=warnings,
    )


# ------------------------------------------------------------------- internals
def _require_key(key_ref: str) -> None:
    if not secrets_store.key_exists(key_ref):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"no private key registered for key_ref={key_ref!r}; POST /keys first",
        )


def _broker_host() -> str:
    try:
        return urlparse(settings.agent_broker_url).hostname or ""
    except ValueError:
        return ""
