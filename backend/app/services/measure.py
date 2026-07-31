"""Automatic measurement of the three device specs (guide §7).

`POST /devices/{id}/probe` already fills `gflops` / `bandwidth_mb_s` /
`latency_ms`, but with whatever is cheapest to obtain: a square matmul, the
NIC's advertised link speed, ICMP to the broker. This module is the accurate
version of the same job, and the one the UI should call instead of asking an
operator to type numbers into the device card.

What "best method" means per field
----------------------------------
gflops
    A 3x3 convolution benchmark, auto-tuned to run for a fixed wall-clock
    budget on whatever hardware answers. The pipeline runs a CNN, and a CNN
    reaches a very different fraction of peak than a big GEMM does -- on most
    GPUs the matmul figure is several times the convolution figure, and the
    simulator turns `gflops` straight into milliseconds (`cum[cut] / gflops`),
    so the optimistic number becomes an optimistic schedule. The matmul is
    still measured and kept in `info` as a cross-check.

    Not measured by timing the deployed shard, which would be more faithful
    still: `head.pt` exists only on the edges and its input shape is known
    (`batch x 3 x imgsz x imgsz`), while `tail.pt` takes the head's feature map,
    whose shape we cannot synthesize here. Measuring the two sides by different
    methods would bias exactly the comparison the split decision rests on.

bandwidth_mb_s
    A timed pull of an incompressible blob the device generates in /tmp,
    over the SSH connection that is already open -- no daemon to install, no
    port to open, and it is the same direction the activations travel (device
    -> here). `iperf3` beats it when an iperf server is actually reachable, so
    it is preferred when `iperf_server` is given. The NIC link speed remains
    the last resort and is flagged, because it is a ceiling and not a
    measurement.

latency_ms
    TCP connect time to the broker's AMQP port, minimum of several attempts.
    A completed TCP handshake is one round trip over the transport the pipeline
    actually uses, and it keeps working on the many networks where ICMP is
    dropped by default (every cloud provider's stock security group). ICMP and
    the SSH round trip stay as fallbacks.

What must not be measured concurrently
--------------------------------------
Latency costs a handful of packets, so it is fanned out. The other two are
measured one device at a time, for the same underlying reason: they consume a
resource the devices may be sharing, so measuring together reports each
device's *share* rather than its capacity.

* **Bandwidth** -- the devices sit behind one uplink and talk to one broker.
* **Compute** -- less obvious, and it cost a wrong answer before it was fixed.
  A matmul looks local to the machine running it, and is, right up until the
  "machines" are VMs on one host, which is how an edge tier is very often
  staged. Measured on a real nine-VM fleet, one box reported 63.1 GFLOPS with
  the host to itself and 28.7 GFLOPS while its eight neighbours benchmarked
  alongside it -- a 2.2x error, silently applied to the field that decides
  where the model gets cut. Nothing here can detect the sharing (the VMs
  cannot see each other), so it is assumed; `serialize_compute=False` opts out
  when the fleet really is separate hardware.

That serialization gives the *solo* figure -- what one device gets with the
link to itself. It is not what a device gets during a run, when every stage
publishes at once. So an optional second pass measures all devices
simultaneously and records the *shared* figure alongside it; the ratio between
them is the contention factor, and it is per-device, so machines on independent
links (ratio ~1.0) separate themselves from machines fighting over one uplink
(ratio well under 1.0) without this module needing to know the network
topology. `bandwidth_basis` decides which of the two lands in the spec field.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import tempfile
import time
import weakref
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import asyncssh

from ..config import BACKEND_ROOT, settings
from ..models import Device
from ..ssh import commands as cmds
from ..ssh.pool import SSHError, SSHPool
from .metrics_bus import bus
from .server_state import server_state

log = logging.getLogger(__name__)

#: Which of the two bandwidth figures lands in the device's spec field --
#: see `resolve_bandwidth`.
BandwidthBasis = Literal["shared", "solo"]


# ------------------------------------------------------------------- constants
#: Peak FP32 GFLOPS by GPU model, used only when nothing can be measured.
#: Vendor peak, so it overstates sustained throughput badly -- always flagged.
GPU_PEAK_GFLOPS: dict[str, float] = {
    "h100": 67_000, "a100": 19_500, "l40s": 91_600, "l4": 30_300,
    "a10g": 31_200, "a10": 31_200, "v100": 15_700, "t4": 8_100,
    "rtx 4090": 82_600, "rtx 4080": 48_700, "rtx 3090": 35_600, "rtx 3080": 29_800,
    "rtx a6000": 38_700, "rtx 2080": 10_100,
    "orin": 5_300, "xavier": 1_400, "jetson": 1_000,
}

#: Wall-clock budget the on-device benchmark tunes its iteration count to.
#: Long enough that clock resolution and one-off allocations wash out, short
#: enough that probing thirty devices is not a coffee break.
COMPUTE_BUDGET_S = 0.8

#: Independent runs averaged into every measured figure.
#:
#: One run is a sample of whatever the machine was doing that second. On
#: co-located VMs -- which is how this fleet is built -- the hypervisor moves
#: that by more than the machines actually differ from each other, so a single
#: shot reports scheduling noise as hardware. Three runs and a mean; the spread
#: between them is kept in `info` so a figure taken during a noisy minute is
#: visible as one rather than quietly believed.
MEASURE_REPEATS = 3

#: Importing torch on a Jetson from cold page cache is genuinely slow.
COMPUTE_TIMEOUT_S = 300

#: Blob size for the transfer test. Starts here; if the pull finishes faster
#: than `BLOB_MIN_SECONDS` the link is quick enough that connection setup
#: dominated the number, so it is repeated at `BLOB_MB_MAX`.
BLOB_MB = 16
BLOB_MB_MAX = 64
BLOB_MIN_SECONDS = 0.4

#: SFTP transfer tuning. The defaults issue one modest request at a time, which
#: measures round-trip latency more than it measures throughput.
SFTP_BLOCK_SIZE = 256 * 1024
SFTP_MAX_REQUESTS = 128

#: Generating and deleting the blob is bounded work; the pull is not.
BLOB_SETUP_TIMEOUT_S = 120
BLOB_PULL_TIMEOUT_S = 300

#: TCP handshakes for the latency figure. Odd count, minimum taken.
TCP_SAMPLES = 7
#: `avg > JITTER_FACTOR * min` is worth telling the operator about: the
#: simulator treats latency as a constant, and a jittery link makes that a lie.
JITTER_FACTOR = 2.0


# ------------------------------------------------------------------ bandwidth lock
#: One lock per event loop rather than a module-level singleton: a `Lock` built
#: at import time binds to whichever loop first awaits it, and the test client
#: builds a fresh loop per test.
_bw_locks: weakref.WeakKeyDictionary[Any, asyncio.Lock] = weakref.WeakKeyDictionary()


def broker_endpoint() -> tuple[str, int]:
    """Where the devices' traffic is headed: `(host, amqp_port)`.

    `server_state` is what the operator configured through the UI and wins;
    the settings URL is the boot-time default the agents are handed. Read
    together so the latency probe cannot end up timing a handshake against one
    broker's host and another's port.
    """
    if server_state.host:
        return server_state.host, server_state.port or 5672
    try:
        parsed = urlparse(settings.agent_broker_url)
        return parsed.hostname or "", parsed.port or 5672
    except ValueError:
        return "", 5672


def _bandwidth_lock() -> asyncio.Lock:
    """The mutex that keeps two solo bandwidth measurements from overlapping."""
    return _loop_lock(_bw_locks)


#: The same treatment for the compute benchmark. It was originally fanned out,
#: on the reasoning that a matmul is local to the machine running it. That is
#: only true when the machines are separate machines: on a fleet of VMs sharing
#: one host -- which is how an edge tier is very often staged -- benchmarking
#: nine of them together has them splitting one CPU, and every reading comes
#: back low. Measured on real hardware, one box reported 63.1 GFLOPS alone and
#: 28.7 while its eight neighbours benchmarked alongside it.
#:
#: There is no way to detect the sharing from here (the VMs cannot see each
#: other), so the safe default is to assume it. A wrong number is worse than a
#: slow measurement: it silently moves the cut layer.
_compute_locks: weakref.WeakKeyDictionary[Any, asyncio.Lock] = weakref.WeakKeyDictionary()


def _compute_lock() -> asyncio.Lock:
    return _loop_lock(_compute_locks)


def _loop_lock(registry: weakref.WeakKeyDictionary) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = registry.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        registry[loop] = lock
    return lock


# --------------------------------------------------------------------- results
@dataclass
class Measurement:
    """Everything one device's measurement pass produced.

    Deliberately not applied to the `Device` row here -- the router decides
    whether this was a dry run, and the fleet pass needs every device's numbers
    in hand before it can pick a `bandwidth_basis`.
    """

    device_id: str
    device_name: str = ""
    ok: bool = False
    gflops: float | None = None
    bandwidth_mb_s: float | None = None
    latency_ms: float | None = None
    #: The link to itself.
    bandwidth_solo_mb_s: float | None = None
    #: The same test with every other device transferring at the same time.
    bandwidth_shared_mb_s: float | None = None
    #: Which method produced each number, e.g. {"gflops": "conv-fp32"}.
    sources: dict[str, str] = field(default_factory=dict)
    info: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def contention_ratio(self) -> float | None:
        """shared / solo. ~1.0 means this device has its link to itself."""
        if not self.bandwidth_solo_mb_s or self.bandwidth_shared_mb_s is None:
            return None
        return round(self.bandwidth_shared_mb_s / self.bandwidth_solo_mb_s, 3)

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "device_name": self.device_name,
            "ok": self.ok,
            "gflops": self.gflops,
            "bandwidth_mb_s": self.bandwidth_mb_s,
            "latency_ms": self.latency_ms,
            "bandwidth_solo_mb_s": self.bandwidth_solo_mb_s,
            "bandwidth_shared_mb_s": self.bandwidth_shared_mb_s,
            "contention_ratio": self.contention_ratio,
            "sources": dict(self.sources),
            "info": dict(self.info),
            "warnings": list(self.warnings),
            "error": self.error,
        }


# ------------------------------------------------------------- benchmark file
#: The GFLOPS benchmark, kept as a real script rather than a string.
#:
#: It is copied to `<remote_bench_dir>/gflops_bench.py` on each device and run
#: there. A file is worth the transfer: it can be read, re-run by hand, and
#: diffed on the device when a number looks wrong, none of which is true of a
#: `python3 -c` blob that exists only for the length of one SSH channel.
BENCH_LOCAL = BACKEND_ROOT / "agent" / "gflops_bench.py"
BENCH_FILENAME = "gflops_bench.py"

#: Bandwidth over the path the pipeline actually uses: a timed publish to the
#: broker. Pushed to the same directory, for the same reasons.
AMQP_BW_LOCAL = BACKEND_ROOT / "agent" / "amqp_bw.py"

#: Latency over that same path: a message published to a queue this device is
#: consuming, timed until it comes back, halved.
AMQP_RTT_LOCAL = BACKEND_ROOT / "agent" / "amqp_rtt.py"


def remote_script_path(filename: str) -> str:
    """Where a pushed script lives on a device.

    Relative by default (`ntuanh/<name>`), so it resolves against the login
    user's home directory and needs no privileged write.
    """
    directory = (settings.remote_bench_dir or "").strip().rstrip("/")
    return f"{directory}/{filename}" if directory else filename


def bench_remote_path() -> str:
    """Where the GFLOPS benchmark lives on a device."""
    return remote_script_path(BENCH_FILENAME)


def bench_source() -> str:
    """The script's text, for the `python3 -c` fallback.

    Read from the same file that gets pushed, so the two delivery paths can
    never drift apart. `python3 -c <src> --batch 16` puts the flags in
    `sys.argv[1:]` exactly as a normal invocation would, so argparse does not
    care which route it arrived by.
    """
    return BENCH_LOCAL.read_text(encoding="utf-8")


def _sha256_local(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def ensure_bench_script(
    conn: asyncssh.SSHClientConnection, m: Measurement
) -> str:
    """The GFLOPS benchmark, on the device. Returns its path, or ''."""
    return await ensure_script(conn, m, BENCH_LOCAL, "bench_script")


async def ensure_script(
    conn: asyncssh.SSHClientConnection,
    m: Measurement,
    local: Path,
    label: str,
) -> str:
    """Make sure `local` is on the device under `remote_bench_dir`.

    Returns its remote path, or ''.

    Skips the transfer when the file is already there with the same contents --
    re-measuring a fleet should not re-copy the same few kilobytes to thirty
    machines. `scp_put` creates the parent directory, so a device that has
    never been touched gets `ntuanh/` made for it.
    """
    if not local.is_file():
        m.warnings.append(f"script missing locally: {local}")
        return ""

    remote = remote_script_path(local.name)
    expected = _sha256_local(local)

    check = await cmds.run_command(conn, f"sha256sum {shlex.quote(remote)}", timeout=30)
    if check.ok and check.stdout.strip().split()[:1] == [expected]:
        m.info[label] = {"path": remote, "state": "already present"}
        return remote

    try:
        sent = await cmds.scp_put(conn, local, remote)
    except (OSError, asyncssh.Error) as exc:
        m.warnings.append(f"could not copy {local.name} to {remote}: {exc}")
        return ""

    verify = await cmds.run_command(conn, f"sha256sum {shlex.quote(remote)}", timeout=30)
    if not (verify.ok and verify.stdout.strip().split()[:1] == [expected]):
        # A truncated or mangled script would fail in a much more confusing way
        # a moment later, so refuse it here where the reason is still obvious.
        m.warnings.append(f"{local.name} copied to {remote} but its checksum does not match")
        return ""

    m.info[label] = {"path": remote, "state": "copied", "bytes": sent}
    return remote


#: TCP handshake timing. `__HOST__` / `__PORT__` / `__TRIES__` substituted as
#: JSON literals.
_TCP_SRC = r"""
import json, socket, time
host, port, tries = __HOST__, __PORT__, __TRIES__
samples, error = [], ""
for _ in range(tries):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(3.0)
    t0 = time.perf_counter()
    try:
        s.connect((host, port))
        samples.append((time.perf_counter() - t0) * 1000.0)
    except Exception as exc:
        error = "%s: %s" % (type(exc).__name__, exc)
    finally:
        try:
            s.close()
        except Exception:
            pass
    time.sleep(0.05)
out = {"ok": bool(samples), "error": error, "tries": tries,
       "samples": [round(x, 3) for x in samples]}
if samples:
    out["min_ms"] = round(min(samples), 3)
    out["avg_ms"] = round(sum(samples) / len(samples), 3)
    out["max_ms"] = round(max(samples), 3)
print(json.dumps(out))
"""


def _render(src: str, **values: Any) -> str:
    """Substitute `__NAME__` placeholders with JSON literals."""
    for name, value in values.items():
        src = src.replace(f"__{name}__", json.dumps(value))
    return src


def last_json(text: str) -> dict[str, Any] | None:
    """Agent/benchmark output can carry warnings before the JSON line."""
    for line in reversed((text or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                parsed = json.loads(line)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                continue
    return None


# ------------------------------------------------------------------- compute
async def measure_compute(
    conn: asyncssh.SSHClientConnection, m: Measurement, *, serialize: bool = True
) -> None:
    """Fill `m.gflops` from an on-device benchmark, or from the GPU's name.

    `serialize=False` only when the caller knows the devices do not share a
    host -- see `_compute_lock`.
    """
    gpu_name, gpu_count = await _gpu_identity(conn, m)

    args = (
        f"--batch {_bench_batch()} --budget {COMPUTE_BUDGET_S} "
        f"--repeat {MEASURE_REPEATS}"
    )
    remote = await ensure_bench_script(conn, m)
    if remote:
        command = f"{settings.remote_python} {shlex.quote(remote)} {args}"
    else:
        # The copy did not take -- a read-only home, no SFTP subsystem, a full
        # disk. The same script still runs over the wire: `python3 -c <src>
        # --batch 16` puts the flags in `sys.argv[1:]` exactly as a file
        # invocation does, so argparse cannot tell the difference.
        command = f"{settings.remote_python} -c {shlex.quote(bench_source())} {args}"
        m.info.setdefault("bench_script", {})["state"] = "inlined (copy failed)"

    # Only the timed run is held: pushing the script and reading nvidia-smi
    # cost the device nothing measurable, and queueing those too would turn a
    # thirty-device pass into a serial crawl for no gain in accuracy.
    if serialize:
        async with _compute_lock():
            bench = await cmds.run_command(conn, command, timeout=COMPUTE_TIMEOUT_S)
    else:
        bench = await cmds.run_command(conn, command, timeout=COMPUTE_TIMEOUT_S)
    parsed = last_json(bench.stdout)

    if parsed and parsed.get("ok"):
        m.info["benchmark"] = parsed
        conv = parsed.get("conv_gflops")
        gemm = parsed.get("gemm_gflops")
        if conv:
            m.gflops = float(conv)
            m.sources["gflops"] = "conv-fp32"
            return
        if gemm:
            m.gflops = float(gemm)
            m.sources["gflops"] = "gemm-fp32"
            m.warnings.append(
                "convolution benchmark produced nothing; using the matmul figure, "
                "which overstates throughput on a CNN workload"
            )
            return

    reason = (parsed or {}).get("error") or bench.error or bench.stderr.strip()[:200]
    m.warnings.append(f"on-device benchmark failed ({reason or 'unknown'})")

    # --- fallback: the vendor's peak for whatever nvidia-smi named ---
    key = next((k for k in GPU_PEAK_GFLOPS if k in gpu_name.lower()), None) if gpu_name else None
    if key:
        m.gflops = GPU_PEAK_GFLOPS[key] * gpu_count
        m.sources["gflops"] = "vendor-peak"
        m.warnings.append(
            f"gflops estimated from vendor peak FP32 for {gpu_name!r} x{gpu_count}; "
            "peak overstates sustained convolution throughput several-fold"
        )


def _bench_batch() -> int:
    """Batch the benchmark runs at.

    Batch size lives on the cluster, not the device, and a device can serve
    more than one -- so rather than pick one cluster's value arbitrarily, use a
    fixed batch large enough to keep a GPU busy. Throughput is flat in batch
    size well before this point, which is the property being measured.
    """
    return 16


async def _gpu_identity(
    conn: asyncssh.SSHClientConnection, m: Measurement
) -> tuple[str, int]:
    gpu = await cmds.run_command(
        conn,
        "nvidia-smi --query-gpu=name,memory.total --format=csv,noheader",
        timeout=30,
    )
    if not (gpu.ok and gpu.stdout.strip()):
        m.warnings.append("nvidia-smi unavailable -- treating as a CPU-only device")
        return "", 0
    lines = [line.strip() for line in gpu.stdout.strip().splitlines() if line.strip()]
    m.info["nvidia_smi"] = gpu.stdout.strip()
    m.info["gpu_count"] = len(lines)
    name = lines[0].split(",")[0].strip()
    m.info["gpu_name"] = name
    return name, len(lines)


# ------------------------------------------------------------------- latency
#: Timed round trips through the broker, and the payload each carries.
AMQP_RTT_TRIES = 10
AMQP_RTT_BYTES = 64
AMQP_RTT_TIMEOUT_S = 120


async def _amqp_latency(
    conn: asyncssh.SSHClientConnection, m: Measurement
) -> dict[str, Any] | None:
    """Publish a message to the broker and back, and halve the round trip.

    Returns the script's report, or None when the broker is not configured,
    pika is missing, or nothing came back -- the caller then falls through to
    the handshake, which measures the network without the broker on top of it.
    """
    from ..ssh import gateway

    try:
        url = gateway.device_amqp_url(await gateway.load_config())
    except Exception as exc:                                  # noqa: BLE001
        m.warnings.append(f"could not resolve the broker URL: {exc}")
        return None
    if not url:
        return None

    script = await ensure_script(conn, m, AMQP_RTT_LOCAL, "amqp_rtt_script")
    if not script:
        return None

    # Credential via the environment: /proc/<pid>/cmdline is world-readable.
    command = (
        f"SPLITINF_AMQP_URL={shlex.quote(url)} "
        f"{settings.remote_python} {shlex.quote(script)} "
        f"--tries {AMQP_RTT_TRIES} --bytes {AMQP_RTT_BYTES}"
    )
    result = await cmds.run_command(conn, command, timeout=AMQP_RTT_TIMEOUT_S)
    parsed = last_json(result.stdout)

    if not (parsed and parsed.get("ok") and parsed.get("one_way_ms") is not None):
        reason = (parsed or {}).get("error") or result.error or result.stderr.strip()[:200]
        m.warnings.append(f"broker round-trip test failed ({reason or 'unknown'})")
        return None

    # The URL never goes into `info`: the API returns it and the UI renders it.
    return {
        k: parsed.get(k)
        for k in ("one_way_ms", "one_way_min_ms", "one_way_std_ms", "rtt_min_ms",
                  "rtt_avg_ms", "rtt_max_ms", "samples", "timeouts",
                  "payload_bytes", "connect_ms", "broker")
    }


async def measure_latency(
    conn: asyncssh.SSHClientConnection, m: Measurement, target: str, port: int
) -> None:
    """Fill `m.latency_ms` with the delay to the broker, best method available."""
    # --- best: a real message, there and back, halved ---
    # A handshake measures the network and stops. This measures what a message
    # costs, which is the network *plus* what RabbitMQ does with it -- and on a
    # LAN the broker-side work is most of the number rather than a rounding
    # error. Same connection and same publish call the edge agent makes.
    if report := await _amqp_latency(conn, m):
        m.latency_ms = round(float(report["one_way_ms"]), 3)
        m.sources["latency"] = "amqp-rtt"
        m.info["amqp_rtt"] = report
        best = float(report.get("one_way_min_ms") or 0.0)
        if best and m.latency_ms > JITTER_FACTOR * best:
            m.warnings.append(
                f"latency is jittery (mean {m.latency_ms} ms one-way against a best "
                f"of {best} ms over {report.get('samples')} messages); the simulator "
                f"treats it as a constant, so a run will not be as smooth as it looks"
            )
        return

    # These commands run *on the device*, where "localhost" is the device. A
    # ping to it succeeds in 0.06 ms and looks like a wonderful network link,
    # which is far worse than failing: it is a plausible number for something
    # that was never measured. `ssh/commands.py` refuses the same target for
    # the iperf3/ping presets, for the same reason.
    if target and target.strip().lower() in cmds.LOOPBACK_HOSTS:
        m.warnings.append(
            f"broker host is {target!r}, which on a device means the device "
            "itself -- no latency to the broker was measured. Set the AMQP host "
            "(broker card) or DEVICE_BROKER_URL to an address the devices can route to."
        )
        target = ""

    if target:
        src = _render(_TCP_SRC, HOST=target, PORT=port, TRIES=TCP_SAMPLES)
        tcp = await cmds.run_command(
            conn, f"{settings.remote_python} -c {shlex.quote(src)}", timeout=60
        )
        parsed = last_json(tcp.stdout)
        if parsed and parsed.get("ok"):
            m.info["tcp_rtt"] = {**parsed, "target": f"{target}:{port}"}
            m.latency_ms = round(float(parsed["min_ms"]), 2)
            m.sources["latency"] = "tcp-connect"
            avg = float(parsed.get("avg_ms") or 0.0)
            if avg > JITTER_FACTOR * m.latency_ms:
                m.warnings.append(
                    f"latency is jittery (min {m.latency_ms} ms, avg {round(avg, 2)} ms); "
                    "the simulator treats it as a constant, so its transfer times "
                    "will be optimistic"
                )
            return
        if parsed:
            m.warnings.append(
                f"TCP handshake to {target}:{port} failed "
                f"({parsed.get('error') or 'no route'}); falling back to ICMP"
            )

        # --- fallback: ICMP, if this network passes it ---
        ping = await cmds.run_command(
            conn, f"ping -c 3 -w 5 {shlex.quote(target)}", timeout=30
        )
        if match := re.search(r"=\s*[\d.]+/([\d.]+)/", ping.stdout or ""):
            m.latency_ms = round(float(match.group(1)), 2)
            m.sources["latency"] = "icmp"
            m.info["ping_target"] = target
            return

    # --- last resort: how long a no-op command takes to come back ---
    #
    # Recorded, but deliberately *not* applied to the spec field. The SSH round
    # trip is the operator's path to the device, which is not the device's path
    # to the broker and can be wildly further: measured on a real fleet reached
    # through a jump host over a WAN tunnel, this came back at 390-578 ms for
    # machines sitting 0.6 ms from their broker. Writing that would not be a
    # rough latency, it would be a fictional one, and `cum[cut]` schedules
    # around it. Leaving the field alone keeps whatever the operator knows.
    rtt = await cmds.run_command(conn, "true", timeout=20)
    m.info["ssh_rtt_ms"] = round(rtt.duration_ms, 2)
    m.sources["latency"] = "ssh-rtt (not applied)"
    m.warnings.append(
        f"no latency to the broker could be measured; the SSH round trip is "
        f"{round(rtt.duration_ms, 2)} ms but that is this console's path to the "
        f"device, not the device's path to the broker, so the latency field was "
        f"left unchanged"
    )


# ----------------------------------------------------------------- bandwidth
def _blob_path(device_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]", "", device_id) or "dev"
    return f"/tmp/.splitinf-bw-{safe}.bin"


async def _make_blob(
    conn: asyncssh.SSHClientConnection, path: str, mb: int
) -> str:
    """Write `mb` MB of incompressible bytes on the device. Returns an error, or ''.

    /dev/urandom rather than /dev/zero: SSH negotiates compression on some
    configurations, and a zero-filled file would travel as almost nothing and
    report a fictional link speed.
    """
    r = await cmds.run_command(
        conn,
        f"dd if=/dev/urandom of={shlex.quote(path)} bs=1M count={int(mb)} 2>/dev/null",
        timeout=BLOB_SETUP_TIMEOUT_S,
    )
    if not r.ok:
        return r.error or r.stderr.strip()[:200] or f"dd exited {r.exit}"
    return ""


async def _remove_blob(conn: asyncssh.SSHClientConnection, path: str) -> None:
    await cmds.run_command(conn, f"rm -f {shlex.quote(path)}", timeout=30)


async def _pull_blob(
    conn: asyncssh.SSHClientConnection, path: str
) -> tuple[float, int]:
    """Time a full read of the blob. Returns (seconds, bytes).

    Pulled to a local temp file rather than streamed into memory because
    `SFTPClient.get` keeps many block requests in flight, and a single
    sequential `read()` loop would measure round-trip latency instead of
    throughput. The local disk is assumed faster than the link -- if that ever
    stops being true, `nic_link_mbit` in `info` is the cross-check that shows it.
    """
    # `mkstemp` hands back an open descriptor; asyncssh opens the path itself,
    # and on Windows the stray handle would also block the unlink below.
    handle, name = tempfile.mkstemp(prefix="splitinf-bw-", suffix=".bin")
    os.close(handle)
    local = Path(name)
    try:
        async with conn.start_sftp_client() as sftp:
            start = time.perf_counter()
            await asyncio.wait_for(
                sftp.get(
                    path,
                    str(local),
                    block_size=SFTP_BLOCK_SIZE,
                    max_requests=SFTP_MAX_REQUESTS,
                ),
                timeout=BLOB_PULL_TIMEOUT_S,
            )
            elapsed = time.perf_counter() - start
        return elapsed, local.stat().st_size
    finally:
        local.unlink(missing_ok=True)


async def _iperf3(
    conn: asyncssh.SSHClientConnection, m: Measurement, server: str
) -> float | None:
    r = await cmds.run_command(
        conn, f"iperf3 -c {shlex.quote(server)} -t 3 -J", timeout=90
    )
    payload = last_json(r.stdout)
    bits = (((payload or {}).get("end") or {}).get("sum_sent") or {}).get("bits_per_second")
    if not bits:
        m.warnings.append(
            f"iperf3 to {server} produced no result "
            f"({r.error or r.stderr.strip()[:120] or 'is iperf3 -s running there?'})"
        )
        return None
    return round(float(bits) / 8e6, 2)  # bits/s -> MB/s


async def _nic_link(
    conn: asyncssh.SSHClientConnection, m: Measurement
) -> float | None:
    link = await cmds.run_command(
        conn, "cat /sys/class/net/*/speed 2>/dev/null | sort -rn | head -n1", timeout=20
    )
    raw = (link.stdout or "").strip().splitlines()
    mbit = next((int(x) for x in raw if x.strip().lstrip("-").isdigit() and int(x) > 0), None)
    if mbit is None:
        return None
    m.info["nic_link_mbit"] = mbit
    return round(mbit / 8.0, 2)  # Mbit/s -> MB/s


async def measure_bandwidth(
    conn: asyncssh.SSHClientConnection,
    m: Measurement,
    *,
    iperf_server: str | None = None,
    serialize: bool = True,
) -> None:
    """Fill `m.bandwidth_solo_mb_s` -- the link with nothing else on it.

    `serialize=False` is for the contention pass, which *wants* the overlap
    this lock exists to prevent. It is the only caller that should pass it.
    """
    # --- best: a timed publish to the broker, run entirely on the device ---
    # This is the path the pipeline uses, so it is the path worth timing, and
    # nothing about it involves this console -- which is what makes it the only
    # method that still works when the fleet is behind a jump host.
    if not iperf_server:
        if serialize:
            async with _bandwidth_lock():
                report = await _amqp_bandwidth(conn, m)
        else:
            report = await _amqp_bandwidth(conn, m)
        if report:
            m.bandwidth_solo_mb_s = float(report["mb_s"])
            m.sources["bandwidth"] = "amqp-publish"
            m.info["amqp_bandwidth"] = report
            return

    # The transfer test pulls to *this* machine. That is the right path only
    # while this machine is on the same network as the devices. Behind a jump
    # host it is not: every byte would cross the operator's tunnel instead of
    # the LAN the activations travel over. Measured on a real fleet, one 16 MB
    # pull took 6m53s -- 0.04 MB/s -- for devices sitting 0.6 ms from their
    # broker. Refused for the same reason the SSH round trip is refused as a
    # latency: a number describing the wrong link is worse than no number, and
    # far worse when it takes an hour to produce.
    # `iperf_server` is exempt: that measures device -> the server the operator
    # named, which never was this console, so the tunnel is not in the path.
    if not iperf_server and await _reached_through_jump_host():
        m.warnings.append(
            "bandwidth not measured: these devices are reached through the jump "
            "host, so a transfer to this console would time the operator's tunnel "
            "rather than the link to the broker. Run an iperf3 server on the "
            "broker host and pass iperf_server to measure the real path."
        )
        m.sources["bandwidth"] = "skipped (jump host)"
        return

    if not serialize:
        await _bandwidth_once(conn, m, iperf_server=iperf_server)
        return
    async with _bandwidth_lock():
        await _bandwidth_once(conn, m, iperf_server=iperf_server)


#: Bytes the AMQP probe publishes, and how it splits them. 2 MB messages sit
#: close to what the pipeline actually sends (`max_message_mb` is 15), and 32 MB
#: is enough to be a measurement rather than a round trip on a LAN.
AMQP_TOTAL_MB = 32.0
AMQP_MSG_MB = 2.0
AMQP_TIMEOUT_S = 180

#: How far ahead the contention pass schedules its synchronized start. Long
#: enough for every device to open a channel, start python and connect first.
CONTENTION_ALIGN_S = 20.0
#: Beyond this the devices did not really overlap, and the figure says so.
CONTENTION_SKEW_WARN_MS = 750.0


async def _amqp_bandwidth(
    conn: asyncssh.SSHClientConnection, m: Measurement, *, start_at: float = 0.0
) -> dict[str, Any] | None:
    """Publish to the broker from the device and time it.

    Returns the script's report, or None when the broker is not configured,
    pika is missing, or the publish failed -- the caller then falls through to
    the older methods, which is what should happen on a fleet this cannot
    serve. Assigning the figure is left to the caller, because the contention
    pass runs the same probe but stores it as the *shared* number.
    """
    from ..ssh import gateway

    try:
        url = gateway.device_amqp_url(await gateway.load_config())
    except Exception as exc:                                  # noqa: BLE001
        m.warnings.append(f"could not resolve the broker URL: {exc}")
        return None
    if not url:
        m.warnings.append(
            "no routable broker host configured, so bandwidth over the real path "
            "could not be measured. Set the AMQP host on the broker card to an "
            "address the devices can reach."
        )
        return None

    script = await ensure_script(conn, m, AMQP_BW_LOCAL, "amqp_bw_script")
    if not script:
        return None

    # The credential goes in the environment, not in argv: /proc/<pid>/cmdline
    # is world-readable on the device and /proc/<pid>/environ is not.
    command = (
        f"SPLITINF_AMQP_URL={shlex.quote(url)} "
        f"{settings.remote_python} {shlex.quote(script)} "
        f"--mb {AMQP_TOTAL_MB} --msg-mb {AMQP_MSG_MB} --repeat {MEASURE_REPEATS}"
        + (f" --start-at {start_at:.3f}" if start_at else "")
    )
    result = await cmds.run_command(conn, command, timeout=AMQP_TIMEOUT_S)
    parsed = last_json(result.stdout)

    if not (parsed and parsed.get("ok") and parsed.get("mb_s")):
        reason = (parsed or {}).get("error") or result.error or result.stderr.strip()[:200]
        m.warnings.append(f"broker publish test failed ({reason or 'unknown'})")
        return None

    # The URL is deliberately not returned: `info` reaches the API and the UI
    # console, and it carries the broker password.
    return {
        k: parsed.get(k)
        for k in ("mb_s", "stats", "mb", "rounds", "messages", "message_mb",
                  "connect_ms", "broker", "start_skew_ms")
    }


async def _reached_through_jump_host() -> bool:
    """True when device connections are tunnelled rather than dialled directly.

    Read from the server config rather than from the connection: asyncssh does
    not advertise a tunnel on the connection object, and the config is the same
    thing `pool._tunnel_for` consults to decide.
    """
    from ..ssh import gateway

    try:
        cfg = await gateway.load_config()
    except Exception:          # noqa: BLE001 - never sink a measurement on this
        return False
    return bool(cfg and cfg.jump_enabled and gateway.is_configured(cfg))


async def _bandwidth_once(
    conn: asyncssh.SSHClientConnection,
    m: Measurement,
    *,
    iperf_server: str | None,
) -> None:
    # --- iperf3, when the operator pointed us at a server ---
    # Only on request: attempting it blind costs 90 seconds per device on every
    # network that has no iperf3 listening, which is most of them.
    if iperf_server:
        if (mb_s := await _iperf3(conn, m, iperf_server)) is not None:
            m.bandwidth_solo_mb_s = mb_s
            m.sources["bandwidth"] = "iperf3"
            m.info["iperf3_mb_s"] = mb_s
            return

    # --- timed pull over the SSH connection that is already open ---
    path = _blob_path(m.device_id)
    error = await _make_blob(conn, path, BLOB_MB)
    if not error:
        try:
            elapsed, size = await _pull_blob(conn, path)
            if elapsed < BLOB_MIN_SECONDS:
                # Too quick to trust: setup dominated. Redo it big enough to
                # actually stress the link.
                await _remove_blob(conn, path)
                error = await _make_blob(conn, path, BLOB_MB_MAX)
                if not error:
                    elapsed, size = await _pull_blob(conn, path)
            if not error and elapsed > 0 and size > 0:
                m.bandwidth_solo_mb_s = round(size / 1e6 / elapsed, 2)
                m.sources["bandwidth"] = "sftp-blob"
                m.info["transfer"] = {
                    "mb": round(size / 1e6, 2),
                    "seconds": round(elapsed, 3),
                    "direction": "device -> control host",
                }
                m.warnings.append(
                    "bandwidth measured over SSH, so it is net of encryption "
                    "overhead on a single stream -- a slight under-estimate of "
                    "the raw link"
                )
                return
        except (OSError, asyncio.TimeoutError, asyncssh.Error) as exc:
            error = f"{type(exc).__name__}: {exc}"
        finally:
            await _remove_blob(conn, path)

    m.warnings.append(f"transfer test failed ({error or 'unknown'})")

    # --- last resort: what the NIC claims it could do ---
    if (mb_s := await _nic_link(conn, m)) is not None:
        m.bandwidth_solo_mb_s = mb_s
        m.sources["bandwidth"] = "nic-link"
        m.warnings.append(
            "bandwidth taken from the NIC link speed, which is a theoretical "
            "ceiling and not a measurement"
        )


# --------------------------------------------------------------- single device
async def measure_device(
    pool: SSHPool,
    device: Device,
    *,
    compute: bool = True,
    bandwidth: bool = True,
    latency: bool = True,
    iperf_server: str | None = None,
    latency_target: str | None = None,
    latency_port: int | None = None,
    serialize_bandwidth: bool = True,
    serialize_compute: bool = True,
    stream: bool = True,
) -> Measurement:
    """Measure one device by the best method each field admits.

    Never raises: a machine that is off comes back as `ok=False` with the
    reason in `error`, because a fleet pass must not lose twenty good results
    to one unplugged Jetson.
    """
    m = Measurement(device_id=device.id, device_name=device.name)
    try:
        conn = await pool.get(device)
    except (SSHError, OSError, asyncssh.Error) as exc:
        m.error = str(exc)
        if stream:
            bus.exec_line(device.id, f"✗ {device.name}: {exc}", "stderr")
        return m

    default_host, default_port = broker_endpoint()
    target = latency_target if latency_target is not None else default_host
    port = latency_port or default_port

    if stream:
        bus.exec_line(device.id, f"┌─ measuring {device.name}", "meta")
    started = time.perf_counter()

    if compute:
        await measure_compute(conn, m, serialize=serialize_compute)
    if latency:
        await measure_latency(conn, m, target, port)
    if bandwidth:
        await measure_bandwidth(
            conn, m, iperf_server=iperf_server, serialize=serialize_bandwidth
        )
        m.bandwidth_mb_s = m.bandwidth_solo_mb_s

    m.ok = True
    m.info["elapsed_s"] = round(time.perf_counter() - started, 2)
    if stream:
        bus.exec_line(device.id, f"└─ {_summary_line(m)}", "stdout")
    return m


def _summary_line(m: Measurement) -> str:
    parts = []
    if m.gflops is not None:
        parts.append(f"{m.gflops:g} GFLOPS ({m.sources.get('gflops', '?')})")
    if m.bandwidth_mb_s is not None:
        parts.append(f"{m.bandwidth_mb_s:g} MB/s ({m.sources.get('bandwidth', '?')})")
    if m.latency_ms is not None:
        parts.append(f"{m.latency_ms:g} ms ({m.sources.get('latency', '?')})")
    return ", ".join(parts) or "nothing measured"


# ----------------------------------------------------------------------- fleet
async def measure_fleet(
    pool: SSHPool,
    devices: list[Device],
    *,
    contention: bool = True,
    iperf_server: str | None = None,
    latency_target: str | None = None,
    latency_port: int | None = None,
    serialize_compute: bool = True,
    stream: bool = True,
) -> tuple[list[Measurement], dict[str, Any]]:
    """Measure a whole fleet, keeping the bandwidth numbers meaningful.

    Three phases, and the ordering is the entire point:

    1. compute + latency, fanned out -- both are local to a device (a matmul
       and a few handshakes), so running them together costs nothing.
    2. solo bandwidth, one device at a time -- serialized by `_bandwidth_lock`.
       Fanning this out would have every device report its *share* of the
       uplink, and a fleet of ten would look ten times slower than it is.
    3. contended bandwidth, everything at once -- the number a device actually
       sees during a run, when every stage is publishing to the broker
       together. Optional, and skipped for devices whose solo figure was not a
       real transfer (`nic-link`), since a ratio against a theoretical ceiling
       would mean nothing.

    Returns `(measurements, summary)`. Nothing is written to the database.
    """
    # --- phase 1: compute + latency, fanned out ---
    results = await _phase_local(
        pool, devices,
        latency_target=latency_target, latency_port=latency_port,
        serialize_compute=serialize_compute, stream=stream,
    )

    # --- phase 2: solo bandwidth, strictly one at a time ---
    by_id = {d.id: d for d in devices}
    for m in results:
        if not m.ok:
            continue
        device = by_id[m.device_id]
        try:
            conn = await pool.get(device)
        except (SSHError, OSError, asyncssh.Error) as exc:
            m.warnings.append(f"bandwidth skipped: {exc}")
            continue
        await measure_bandwidth(conn, m, iperf_server=iperf_server, serialize=True)
        m.bandwidth_mb_s = m.bandwidth_solo_mb_s
        if stream:
            bus.exec_line(
                m.device_id,
                f"   solo bandwidth: {m.bandwidth_solo_mb_s or 0:g} MB/s "
                f"({m.sources.get('bandwidth', 'n/a')})",
                "stdout",
            )

    summary: dict[str, Any] = {
        "devices": len(results),
        "measured": sum(1 for m in results if m.ok),
        "contention_pass": False,
    }

    # --- phase 3: everyone at once ---
    eligible = [
        m for m in results
        if m.ok and m.sources.get("bandwidth") in ("sftp-blob", "amqp-publish")
    ]
    if contention and len(eligible) >= 2:
        await _phase_contention(pool, by_id, eligible, stream=stream)
        summary["contention_pass"] = True
        summary["aggregate_shared_mb_s"] = round(
            sum(m.bandwidth_shared_mb_s or 0.0 for m in eligible), 2
        )
        summary["aggregate_solo_mb_s"] = round(
            sum(m.bandwidth_solo_mb_s or 0.0 for m in eligible), 2
        )
        ratios = [r for m in eligible if (r := m.contention_ratio) is not None]
        if ratios:
            summary["worst_contention_ratio"] = min(ratios)
    elif contention:
        summary["contention_skipped"] = (
            "needs at least two devices with a measured transfer figure"
        )

    return results, summary


async def _phase_local(
    pool: SSHPool,
    devices: list[Device],
    *,
    latency_target: str | None,
    latency_port: int | None,
    serialize_compute: bool,
    stream: bool,
) -> list[Measurement]:
    """Phase 1: latency concurrently; the benchmark queued behind `_compute_lock`.

    Fanned out at this level so the SSH work, the script push and the handshakes
    all overlap. The one thing that must not overlap is the timed benchmark
    itself, and `measure_compute` holds the lock for exactly that.
    """
    sem = asyncio.Semaphore(max(1, settings.fanout_concurrency))

    async def one(d: Device) -> Measurement:
        async with sem:
            return await measure_device(
                pool, d,
                bandwidth=False,
                latency_target=latency_target,
                latency_port=latency_port,
                serialize_compute=serialize_compute,
                stream=stream,
            )

    return list(await asyncio.gather(*(one(d) for d in devices)))


async def _phase_contention(
    pool: SSHPool,
    by_id: dict[str, Device],
    eligible: list[Measurement],
    *,
    stream: bool,
) -> None:
    """Phase 3: the same transfer on every device simultaneously.

    Split into prepare-then-pull so the timed part of every device's transfer
    is in flight at the same moment. Generating the blobs inside the timed
    section would stagger the starts by however long the slowest `dd` took,
    and the fast devices would spend part of their window alone on the link --
    which is exactly the measurement error this pass exists to expose.

    Holds the fleet bandwidth lock for the whole pass. The lock's usual job is
    to keep transfers apart, and this is the one caller that wants them
    together -- but a stray `/devices/{id}/measure` arriving mid-pass would add
    a transfer nobody accounted for, and both the solo figure it reports and
    the ratios measured here would be wrong.
    """
    async with _bandwidth_lock():
        await _run_contention(pool, by_id, eligible, stream=stream)


async def _run_contention(
    pool: SSHPool,
    by_id: dict[str, Device],
    eligible: list[Measurement],
    *,
    stream: bool,
) -> None:
    if stream:
        bus.exec_line(
            eligible[0].device_id,
            f"── contention pass: {len(eligible)} devices transferring together",
            "meta",
        )

    # Every device has to be measured the same way it was measured alone, or
    # the ratio compares two different experiments rather than two conditions.
    amqp = [m for m in eligible if m.sources.get("bandwidth") == "amqp-publish"]
    blob = [m for m in eligible if m.sources.get("bandwidth") == "sftp-blob"]

    if amqp:
        await _contend_amqp(pool, by_id, amqp)
    if blob:
        await _contend_blob(pool, by_id, blob)

    _report_contention(eligible, stream=stream)


async def _contend_amqp(
    pool: SSHPool, by_id: dict[str, Device], group: list[Measurement]
) -> None:
    """Every device publishing to the broker at once.

    Closer to the real thing than the file-transfer version ever was: this is
    literally what a running pipeline does, so the ratio it produces is the
    broker's ingest ceiling shared out, not an analogy for it.
    """

    # Every device is told the same wall-clock instant to begin at, and waits
    # for it. Launching them with `gather` is not enough: each one still has to
    # open an SSH channel, start python, import pika and connect, and those do
    # not finish together -- so the first to arrive would publish part of its
    # data with the broker to itself and report a throughput nobody else sees.
    start_at = time.time() + CONTENTION_ALIGN_S

    async def one(m: Measurement) -> None:
        try:
            conn = await pool.get(by_id[m.device_id])
        except (SSHError, OSError, asyncssh.Error) as exc:
            m.warnings.append(f"contention pass skipped: {exc}")
            return
        report = await _amqp_bandwidth(conn, m, start_at=start_at)
        if report and report.get("mb_s"):
            m.bandwidth_shared_mb_s = float(report["mb_s"])
            skew = float(report.get("start_skew_ms") or 0.0)
            if abs(skew) > CONTENTION_SKEW_WARN_MS:
                m.warnings.append(
                    f"this device began the contention test {round(skew)} ms off the "
                    f"agreed start (clock skew, or a slow connect), so its shared "
                    f"bandwidth overlapped the others less than intended"
                )

    await asyncio.gather(*(one(m) for m in group))


async def _contend_blob(
    pool: SSHPool, by_id: dict[str, Device], eligible: list[Measurement]
) -> None:
    conns: dict[str, asyncssh.SSHClientConnection] = {}
    ready: list[Measurement] = []

    async def prepare(m: Measurement) -> None:
        try:
            conn = await pool.get(by_id[m.device_id])
        except (SSHError, OSError, asyncssh.Error) as exc:
            m.warnings.append(f"contention pass skipped: {exc}")
            return
        size = BLOB_MB_MAX if (m.bandwidth_solo_mb_s or 0) > 40 else BLOB_MB
        if error := await _make_blob(conn, _blob_path(m.device_id), size):
            m.warnings.append(f"contention pass skipped: {error}")
            return
        conns[m.device_id] = conn
        ready.append(m)

    await asyncio.gather(*(prepare(m) for m in eligible))

    if len(ready) < 2:
        for m in ready:
            await _remove_blob(conns[m.device_id], _blob_path(m.device_id))
        return

    async def pull(m: Measurement) -> None:
        conn = conns[m.device_id]
        try:
            elapsed, size = await _pull_blob(conn, _blob_path(m.device_id))
            if elapsed > 0 and size > 0:
                m.bandwidth_shared_mb_s = round(size / 1e6 / elapsed, 2)
        except (OSError, asyncio.TimeoutError, asyncssh.Error) as exc:
            m.warnings.append(f"contention pass failed: {type(exc).__name__}: {exc}")
        finally:
            await _remove_blob(conn, _blob_path(m.device_id))

    await asyncio.gather(*(pull(m) for m in ready))


def _report_contention(ready: list[Measurement], *, stream: bool) -> None:
    measured = [m for m in ready if m.bandwidth_shared_mb_s is not None]
    for m in ready:
        ratio = m.contention_ratio
        if ratio is None:
            continue
        m.info["contention"] = {
            "solo_mb_s": m.bandwidth_solo_mb_s,
            "shared_mb_s": m.bandwidth_shared_mb_s,
            "ratio": ratio,
            "method": m.sources.get("bandwidth"),
            "concurrent_devices": len(measured),
        }
        if ratio < 0.8:
            m.warnings.append(
                f"under load this device gets {ratio:.0%} of its solo bandwidth "
                f"({m.bandwidth_shared_mb_s:g} vs {m.bandwidth_solo_mb_s:g} MB/s) -- "
                f"it shares the path to the broker with the other "
                f"{len(measured) - 1} device(s)"
            )
        if stream:
            bus.exec_line(
                m.device_id,
                f"   shared bandwidth: {m.bandwidth_shared_mb_s or 0:g} MB/s "
                f"({ratio:.0%} of solo)",
                "stdout",
            )


# ------------------------------------------------------------------- applying
def resolve_bandwidth(m: Measurement, basis: BandwidthBasis) -> float | None:
    """The figure that belongs in the device's `bandwidth_mb_s` spec field.

    `shared` is the default because the simulator's transfer times describe a
    *running* pipeline, and in a running pipeline every stage publishes at
    once -- so the contended figure is the one the schedule should be built on.
    It falls back to solo when no contention pass ran, which is also the right
    answer there: with nothing to contend with, solo is the operating figure.

    `solo` is for sizing the link itself ("did this Jetson negotiate gigabit or
    100 Mbit?"), where another device's traffic is noise rather than signal.
    """
    if basis == "solo":
        return m.bandwidth_solo_mb_s
    return m.bandwidth_shared_mb_s if m.bandwidth_shared_mb_s is not None else m.bandwidth_solo_mb_s


def apply_to_device(device: Device, m: Measurement, basis: BandwidthBasis = "shared") -> None:
    """Write a measurement onto the device row. Unmeasured fields are left alone.

    Leaving them alone matters: a device where torch is missing should keep
    whatever GFLOPS an operator typed, not have it zeroed by a failed probe.
    """
    if m.gflops is not None:
        device.gflops = m.gflops
    if (bw := resolve_bandwidth(m, basis)) is not None:
        device.bandwidth_mb_s = bw
        m.bandwidth_mb_s = bw
    if m.latency_ms is not None:
        device.latency_ms = m.latency_ms
    device.probed_at = datetime.now(timezone.utc)
    device.probe_info = {
        **m.info,
        "sources": dict(m.sources),
        "bandwidth_basis": basis,
        "bandwidth_solo_mb_s": m.bandwidth_solo_mb_s,
        "bandwidth_shared_mb_s": m.bandwidth_shared_mb_s,
        "warnings": list(m.warnings),
    }
