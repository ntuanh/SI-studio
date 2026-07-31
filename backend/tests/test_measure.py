"""Automatic spec measurement (`app/services/measure.py`).

The load-bearing property here is not any single number -- it is *when* the
numbers are taken. Bandwidth measured on twenty devices at once is twenty
readings of one shared link, so the solo pass has to be serialized and the
contention pass has to genuinely overlap. Both are asserted below by recording
the intervals each transfer occupied and checking them for overlap.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from app.models import Device
from app.services import measure


# ------------------------------------------------------------------- fixtures
class FakePool:
    """Stands in for `SSHPool`: hands out a sentinel per device, or raises."""

    def __init__(self, failing: set[str] | None = None) -> None:
        self.failing = failing or set()
        self.handed_out: list[str] = []

    async def get(self, device: Device):
        if device.id in self.failing:
            raise OSError(f"cannot reach {device.id}")
        self.handed_out.append(device.id)
        return f"conn-{device.id}"


def make_devices(n: int) -> list[Device]:
    return [
        Device(id=f"d{i}", name=f"dev-{i}", kind="Edge" if i % 2 else "Cloud", host=f"10.0.0.{i}")
        for i in range(n)
    ]


class Recorder:
    """Records the [start, end] interval of every transfer it stands in for."""

    def __init__(self, duration: float = 0.05, mb: float = 16.0) -> None:
        self.duration = duration
        self.mb = mb
        self.intervals: list[tuple[str, float, float]] = []

    async def pull(self, conn, path: str):
        started = time.perf_counter()
        await asyncio.sleep(self.duration)
        ended = time.perf_counter()
        self.intervals.append((path, started, ended))
        return self.duration, int(self.mb * 1e6)

    def overlaps(self) -> int:
        """How many pairs of transfers were in flight at the same time."""
        pairs = 0
        for i, (_, a0, a1) in enumerate(self.intervals):
            for _, b0, b1 in self.intervals[i + 1:]:
                if a0 < b1 and b0 < a1:
                    pairs += 1
        return pairs


@pytest.fixture()
def quiet_phases(monkeypatch):
    """Skip compute/latency -- those phases have their own tests."""

    async def _noop(conn, m, *args, **kwargs):
        m.gflops = 100.0
        m.sources["gflops"] = "conv-fp32"
        m.latency_ms = 1.0
        m.sources["latency"] = "tcp-connect"

    monkeypatch.setattr(measure, "measure_compute", _noop)
    monkeypatch.setattr(measure, "measure_latency", _noop)


@pytest.fixture()
def fake_transfer(monkeypatch):
    """Replace the blob lifecycle with an instrumented, instant version."""
    rec = Recorder()

    async def make_blob(conn, path, mb):
        return ""

    async def remove_blob(conn, path):
        return None

    monkeypatch.setattr(measure, "_make_blob", make_blob)
    monkeypatch.setattr(measure, "_remove_blob", remove_blob)
    monkeypatch.setattr(measure, "_pull_blob", rec.pull)
    return rec


# ------------------------------------------------- the reason this module exists
def test_solo_bandwidth_is_never_measured_on_two_devices_at_once(
    quiet_phases, fake_transfer
):
    """The whole point of phase 2.

    Six devices sharing one uplink, measured together, would each report a
    sixth of it. The fleet pass must hold them to one at a time.
    """
    devices = make_devices(6)
    results, summary = asyncio.run(
        measure.measure_fleet(FakePool(), devices, contention=False, stream=False)
    )

    assert summary["measured"] == 6
    assert fake_transfer.overlaps() == 0, "solo transfers overlapped -- the lock is not holding"
    assert all(m.bandwidth_solo_mb_s and m.bandwidth_solo_mb_s > 0 for m in results)
    assert all(m.sources["bandwidth"] == "sftp-blob" for m in results)


def test_the_contention_pass_does_overlap_and_reports_the_ratio(
    quiet_phases, monkeypatch
):
    """Phase 3 wants the overlap phase 2 forbids, and must report what it cost.

    The stand-in halves each device's throughput while others are transferring,
    which is what a shared uplink does -- so the ratio has to come back at
    ~0.5 and the warning has to fire.
    """
    rec = Recorder()
    in_flight = 0

    async def pull(conn, path):
        nonlocal in_flight
        in_flight += 1
        try:
            # Yield first so every concurrent caller has registered before
            # anyone decides whether it is sharing the link.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            # Solo: 16 MB in 50 ms. Contended: the same bytes take twice as long.
            slow = in_flight > 1
            started = time.perf_counter()
            await asyncio.sleep(0.1 if slow else 0.05)
            rec.intervals.append((path, started, time.perf_counter()))
            return (0.1 if slow else 0.05), 16_000_000
        finally:
            in_flight -= 1

    async def noop(*args, **kwargs):
        return ""

    monkeypatch.setattr(measure, "_make_blob", noop)
    monkeypatch.setattr(measure, "_remove_blob", noop)
    monkeypatch.setattr(measure, "_pull_blob", pull)

    devices = make_devices(4)
    results, summary = asyncio.run(
        measure.measure_fleet(FakePool(), devices, contention=True, stream=False)
    )

    assert summary["contention_pass"] is True
    assert rec.overlaps() > 0, "the contention pass ran its transfers one at a time"

    for m in results:
        assert m.bandwidth_solo_mb_s == pytest.approx(320.0, rel=0.01)
        assert m.bandwidth_shared_mb_s == pytest.approx(160.0, rel=0.01)
        assert m.contention_ratio == pytest.approx(0.5, rel=0.05)
        assert any("solo bandwidth" in w for w in m.warnings)
        assert m.info["contention"]["concurrent_devices"] == 4

    assert summary["aggregate_shared_mb_s"] == pytest.approx(640.0, rel=0.01)
    assert summary["worst_contention_ratio"] == pytest.approx(0.5, rel=0.05)


def test_the_contention_pass_holds_the_fleet_lock_while_it_overlaps(
    quiet_phases, fake_transfer, monkeypatch
):
    """The pass wants its transfers together, but only *its* transfers.

    A `/devices/{id}/measure` landing mid-pass would add traffic nobody
    accounted for, and both its solo figure and every ratio measured here would
    be wrong -- so the pass keeps the lock for its whole duration.
    """
    in_contention = {"now": False}
    observed: list[bool] = []

    real_pull = fake_transfer.pull

    async def pull(conn, path):
        if in_contention["now"]:
            observed.append(measure._bandwidth_lock().locked())
        return await real_pull(conn, path)

    real_run = measure._run_contention

    async def watched(*args, **kwargs):
        in_contention["now"] = True
        try:
            return await real_run(*args, **kwargs)
        finally:
            in_contention["now"] = False

    monkeypatch.setattr(measure, "_pull_blob", pull)
    monkeypatch.setattr(measure, "_run_contention", watched)

    asyncio.run(
        measure.measure_fleet(FakePool(), make_devices(3), contention=True, stream=False)
    )
    assert observed and all(observed), "the contention pass ran without the lock held"


def test_a_single_device_measure_still_takes_the_fleet_lock(quiet_phases, fake_transfer):
    """A one-off `/devices/{id}/measure` fired during a fleet pass must queue,
    not slip in beside it and spoil both numbers."""
    devices = make_devices(3)
    pool = FakePool()

    async def scenario():
        fleet = asyncio.create_task(
            measure.measure_fleet(pool, devices, contention=False, stream=False)
        )
        await asyncio.sleep(0)  # let the fleet pass reach its first transfer
        single = await measure.measure_device(
            pool, Device(id="dX", name="late", host="10.0.0.9"), stream=False
        )
        await fleet
        return single

    single = asyncio.run(scenario())
    assert single.ok
    assert fake_transfer.overlaps() == 0


def test_contention_is_skipped_when_there_is_nothing_to_contend_with(
    quiet_phases, fake_transfer
):
    devices = make_devices(1)
    _, summary = asyncio.run(
        measure.measure_fleet(FakePool(), devices, contention=True, stream=False)
    )
    assert summary["contention_pass"] is False
    assert "at least two devices" in summary["contention_skipped"]


def test_an_unreachable_device_does_not_sink_the_fleet_pass(quiet_phases, fake_transfer):
    devices = make_devices(4)
    results, summary = asyncio.run(
        measure.measure_fleet(
            FakePool(failing={"d1"}), devices, contention=False, stream=False
        )
    )
    assert summary["measured"] == 3
    dead = next(m for m in results if m.device_id == "d1")
    assert not dead.ok and "cannot reach d1" in dead.error
    assert all(m.ok for m in results if m.device_id != "d1")


# ------------------------------------------------------------ which figure wins
def test_resolve_bandwidth_picks_the_operating_figure_by_default():
    m = measure.Measurement(device_id="d", bandwidth_solo_mb_s=100.0,
                            bandwidth_shared_mb_s=25.0)
    assert measure.resolve_bandwidth(m, "shared") == 25.0
    assert measure.resolve_bandwidth(m, "solo") == 100.0


def test_shared_falls_back_to_solo_when_no_contention_pass_ran():
    m = measure.Measurement(device_id="d", bandwidth_solo_mb_s=100.0)
    assert measure.resolve_bandwidth(m, "shared") == 100.0
    assert m.contention_ratio is None


def test_apply_leaves_unmeasured_fields_alone():
    """A device without torch keeps whatever GFLOPS an operator typed."""
    d = Device(id="d", name="d", gflops=472.0, bandwidth_mb_s=12.0, latency_ms=6.0)
    m = measure.Measurement(
        device_id="d", ok=True, latency_ms=3.5,
        bandwidth_solo_mb_s=90.0, bandwidth_shared_mb_s=30.0,
        sources={"latency": "tcp-connect", "bandwidth": "sftp-blob"},
    )
    measure.apply_to_device(d, m, "shared")

    assert d.gflops == 472.0            # untouched: nothing measured it
    assert d.bandwidth_mb_s == 30.0     # the contended figure
    assert d.latency_ms == 3.5
    assert d.probed_at is not None
    assert d.probe_info["bandwidth_basis"] == "shared"
    assert d.probe_info["bandwidth_solo_mb_s"] == 90.0
    assert d.probe_info["sources"]["latency"] == "tcp-connect"


# ------------------------------------------------------- shipping the benchmark
class FakeConn:
    """Records the commands and file pushes a measurement performs."""

    def __init__(self, remote_sha: str | None = None) -> None:
        self.remote_sha = remote_sha        # None -> the file is not there yet
        self.commands: list[str] = []
        self.pushes: list[tuple[str, str]] = []


def bench_conn(monkeypatch, conn: FakeConn):
    """Wire `cmds.run_command` / `cmds.scp_put` to a FakeConn."""

    async def run_command(_conn, cmd, timeout=None):
        conn.commands.append(cmd)
        out = ""
        if cmd.startswith("sha256sum") and conn.remote_sha:
            out = f"{conn.remote_sha}  {cmd.split()[-1]}"
        return measure.cmds.CmdResult(
            device_id="d", command=cmd, stdout=out, exit=0 if out or "sha256sum" not in cmd else 1
        )

    async def scp_put(_conn, local, remote):
        conn.pushes.append((str(local), remote))
        conn.remote_sha = measure._sha256_local(Path(local))   # it is there now
        return Path(local).stat().st_size

    monkeypatch.setattr(measure.cmds, "run_command", run_command)
    monkeypatch.setattr(measure.cmds, "scp_put", scp_put)


def test_the_benchmark_is_copied_into_the_configured_directory(monkeypatch):
    conn = FakeConn()
    bench_conn(monkeypatch, conn)
    m = measure.Measurement(device_id="d")

    remote = asyncio.run(measure.ensure_bench_script(conn, m))

    assert remote == "ntuanh/gflops_bench.py"
    assert conn.pushes == [(str(measure.BENCH_LOCAL), "ntuanh/gflops_bench.py")]
    assert m.info["bench_script"]["state"] == "copied"
    assert not m.warnings


def test_an_unchanged_benchmark_is_not_copied_again(monkeypatch):
    """Re-measuring a fleet must not re-send the same file to thirty machines."""
    conn = FakeConn(remote_sha=measure._sha256_local(measure.BENCH_LOCAL))
    bench_conn(monkeypatch, conn)
    m = measure.Measurement(device_id="d")

    remote = asyncio.run(measure.ensure_bench_script(conn, m))

    assert remote == "ntuanh/gflops_bench.py"
    assert conn.pushes == [], "the file was already there and was copied anyway"
    assert m.info["bench_script"]["state"] == "already present"


def test_a_stale_benchmark_is_replaced(monkeypatch):
    conn = FakeConn(remote_sha="0" * 64)
    bench_conn(monkeypatch, conn)
    m = measure.Measurement(device_id="d")

    asyncio.run(measure.ensure_bench_script(conn, m))
    assert conn.pushes, "an out-of-date script on the device was left in place"


def test_the_directory_is_configurable(monkeypatch):
    monkeypatch.setattr(measure.settings, "remote_bench_dir", "tools/bench")
    assert measure.bench_remote_path() == "tools/bench/gflops_bench.py"
    monkeypatch.setattr(measure.settings, "remote_bench_dir", "")
    assert measure.bench_remote_path() == "gflops_bench.py"


def test_a_failed_copy_falls_back_to_running_it_over_the_wire(monkeypatch):
    """A read-only home must not cost the GFLOPS figure entirely."""
    conn = FakeConn()
    bench_conn(monkeypatch, conn)

    async def refuse(_conn, local, remote):
        raise OSError("Permission denied")

    monkeypatch.setattr(measure.cmds, "scp_put", refuse)
    m = measure.Measurement(device_id="d")

    asyncio.run(measure.measure_compute(conn, m))

    ran = [c for c in conn.commands if "gflops_bench" in c or " -c " in c]
    assert any(" -c " in c for c in ran), "no inline fallback was attempted"
    assert any("--batch" in c for c in ran), "the flags were dropped on the way"
    assert any("Permission denied" in w for w in m.warnings)
    assert m.info["bench_script"]["state"] == "inlined (copy failed)"


def test_the_pushed_script_is_the_one_that_runs(monkeypatch):
    conn = FakeConn()
    bench_conn(monkeypatch, conn)
    m = measure.Measurement(device_id="d")

    asyncio.run(measure.measure_compute(conn, m))

    invocation = [c for c in conn.commands if "gflops_bench.py" in c and "sha256sum" not in c]
    assert invocation, "the copied script was never invoked"
    assert "ntuanh/gflops_bench.py" in invocation[0]
    assert "--batch 16" in invocation[0] and "--budget" in invocation[0]


def test_the_inline_fallback_is_the_same_source_as_the_file():
    """Two delivery paths, one script -- otherwise they drift and only one of
    them is ever the thing that was tested."""
    assert measure.bench_source() == measure.BENCH_LOCAL.read_text(encoding="utf-8")


# -------------------------------------------------------- the on-device scripts
def test_the_benchmark_script_reports_the_throughput_it_actually_saw(tmp_path: Path):
    """Run `agent/gflops_bench.py` against a stub torch whose ops take a known time.

    This is the arithmetic that decides every device's GFLOPS, and it is
    arithmetic no CI machine has a GPU to check. Pinning the op duration makes
    the expected answer exact: FLOPs-per-call / seconds-per-call.
    """
    seconds_per_call = 0.01
    (tmp_path / "torch").mkdir()
    (tmp_path / "torch" / "__init__.py").write_text(
        textwrap.dedent(
            f"""
            import time, types
            __version__ = "0.0-stub"
            DELAY = {seconds_per_call}

            class T:
                def __matmul__(self, other):
                    time.sleep(DELAY)
                    return self

            def randn(*shape, device=None):
                return T()

            class _NoGrad:
                def __enter__(self): return self
                def __exit__(self, *a): return False

            def no_grad():
                return _NoGrad()

            cuda = types.SimpleNamespace(
                is_available=lambda: False,
                synchronize=lambda: None,
                get_device_name=lambda i: "stub",
            )
            backends = types.SimpleNamespace(cudnn=types.SimpleNamespace(benchmark=False))
            """
        ),
        encoding="utf-8",
    )
    (tmp_path / "torch" / "nn").mkdir()
    (tmp_path / "torch" / "nn" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "torch" / "nn" / "functional.py").write_text(
        textwrap.dedent(
            f"""
            import time
            def conv2d(x, w, padding=0):
                time.sleep({seconds_per_call})
                return x
            """
        ),
        encoding="utf-8",
    )

    # Running a script by path puts the *script's* directory on sys.path, not
    # the cwd, so the stub has to be advertised explicitly.
    import os
    env = {**os.environ, "PYTHONPATH": str(tmp_path)}
    proc = subprocess.run(
        [sys.executable, str(measure.BENCH_LOCAL), "--batch", "16", "--budget", "0.2"],
        capture_output=True, text=True, timeout=120, env=env,
    )
    parsed = measure.last_json(proc.stdout)
    assert parsed and parsed["ok"], proc.stdout + proc.stderr

    # CPU path: 32 channels, 64x64, batch capped at 8.
    n, ch, hw = 8, 32, 64
    assert parsed["conv_shape"] == [n, ch, hw, hw]
    expected_conv = 2.0 * 9 * ch * ch * hw * hw * n / seconds_per_call / 1e9
    assert parsed["conv_gflops"] == pytest.approx(expected_conv, rel=0.25)

    m = parsed["gemm_n"]
    expected_gemm = 2.0 * m**3 / seconds_per_call / 1e9
    assert parsed["gemm_gflops"] == pytest.approx(expected_gemm, rel=0.25)


def test_the_benchmark_script_always_answers_with_one_json_line(tmp_path: Path):
    """Whatever the device is missing, the caller reads one parseable line.

    A box without torch is the common case and must come back as data rather
    than a traceback on stderr -- `last_json` is the only thing reading this,
    and a non-zero exit would lose the reason with it.
    """
    proc = subprocess.run(
        [sys.executable, str(measure.BENCH_LOCAL), "--batch", "16", "--budget", "0.1"],
        capture_output=True, text=True, timeout=300, cwd=str(tmp_path),
    )
    assert proc.returncode == 0, proc.stderr
    parsed = measure.last_json(proc.stdout)
    assert parsed is not None
    assert parsed["ok"] or parsed["error"]


def test_the_latency_script_times_a_real_handshake():
    """`_TCP_SRC` against a socket that accepts, and one that refuses."""
    import socket
    import threading

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    srv.settimeout(0.2)
    port = srv.getsockname()[1]
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
                conn.close()
            except OSError:
                pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        src = measure._render(measure._TCP_SRC, HOST="127.0.0.1", PORT=port, TRIES=5)
        proc = subprocess.run([sys.executable, "-c", src], capture_output=True,
                              text=True, timeout=60)
        parsed = measure.last_json(proc.stdout)
        assert parsed and parsed["ok"]
        assert len(parsed["samples"]) == 5
        assert parsed["min_ms"] <= parsed["avg_ms"] <= parsed["max_ms"]
    finally:
        stop.set()
        thread.join(timeout=2)
        srv.close()

    # A refused port is a measurement failure, reported as data.
    src = measure._render(measure._TCP_SRC, HOST="127.0.0.1", PORT=9, TRIES=2)
    proc = subprocess.run([sys.executable, "-c", src], capture_output=True,
                          text=True, timeout=60)
    parsed = measure.last_json(proc.stdout)
    assert parsed and parsed["ok"] is False and parsed["error"]


def test_scripts_are_injection_proof():
    """Placeholders are substituted as JSON literals, so a hostile hostname
    cannot close the string it sits in."""
    src = measure._render(measure._TCP_SRC, HOST='"; import os; os.system("x") #',
                          PORT=5672, TRIES=1)
    compile(src, "tcp", "exec")  # raises SyntaxError if the quoting broke


# ------------------------------------------------------------------- endpoints
def _add_device(client, auth, device_id: str) -> None:
    r = client.post(
        "/devices",
        json={"id": device_id, "name": device_id, "kind": "Edge", "host": "10.0.0.1"},
        headers=auth,
    )
    assert r.status_code == 201, r.text


def test_measure_endpoint_writes_the_contended_figure_by_default(client, auth, monkeypatch):
    _add_device(client, auth, "dA")
    _add_device(client, auth, "dB")

    async def fake_fleet(pool, devices, **kwargs):
        results = [
            measure.Measurement(
                device_id=d.id, device_name=d.name, ok=True, gflops=1500.0,
                latency_ms=2.5, bandwidth_solo_mb_s=100.0, bandwidth_shared_mb_s=40.0,
                sources={"gflops": "conv-fp32", "bandwidth": "sftp-blob",
                         "latency": "tcp-connect"},
            )
            for d in devices
        ]
        return results, {"devices": len(devices), "measured": len(devices),
                         "contention_pass": True, "worst_contention_ratio": 0.4}

    monkeypatch.setattr(measure, "measure_fleet", fake_fleet)

    r = client.post("/devices/measure", json={}, headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["applied"] is True
    assert body["bandwidth_basis"] == "shared"
    assert body["summary"]["contention_pass"] is True
    assert [m["contention_ratio"] for m in body["results"]] == [0.4, 0.4]

    stored = {d["id"]: d for d in client.get("/devices", headers=auth).json()}
    assert stored["dA"]["bandwidth_mb_s"] == 40.0     # shared, not solo
    assert stored["dA"]["bw"] == 40.0                 # UI alias follows
    assert stored["dA"]["gflops"] == 1500.0
    assert stored["dA"]["probe_info"]["bandwidth_solo_mb_s"] == 100.0


def test_measure_endpoint_honours_the_solo_basis(client, auth, monkeypatch):
    _add_device(client, auth, "dA")

    async def fake_fleet(pool, devices, **kwargs):
        return [
            measure.Measurement(device_id="dA", ok=True, bandwidth_solo_mb_s=100.0,
                                bandwidth_shared_mb_s=40.0)
        ], {"measured": 1}

    monkeypatch.setattr(measure, "measure_fleet", fake_fleet)
    r = client.post("/devices/measure", json={"bandwidth_basis": "solo"}, headers=auth)
    assert r.status_code == 200
    stored = client.get("/devices", headers=auth).json()[0]
    assert stored["bandwidth_mb_s"] == 100.0


def test_measure_endpoint_rejects_an_unknown_basis(client, auth):
    _add_device(client, auth, "dA")
    r = client.post("/devices/measure", json={"bandwidth_basis": "median"}, headers=auth)
    assert r.status_code == 400
    assert "bandwidth_basis" in r.json()["detail"]


def test_measure_endpoint_can_target_a_subset(client, auth, monkeypatch):
    _add_device(client, auth, "dA")
    _add_device(client, auth, "dB")
    seen: list[str] = []

    async def fake_fleet(pool, devices, **kwargs):
        seen.extend(d.id for d in devices)
        return [], {"measured": 0}

    monkeypatch.setattr(measure, "measure_fleet", fake_fleet)
    r = client.post("/devices/measure", json={"device_ids": ["dB"]}, headers=auth)
    assert r.status_code == 200
    assert seen == ["dB"]


def test_measure_endpoint_dry_run_leaves_the_row_alone(client, auth, monkeypatch):
    _add_device(client, auth, "dA")

    async def fake_fleet(pool, devices, **kwargs):
        return [measure.Measurement(device_id="dA", ok=True, gflops=999.0)], {"measured": 1}

    monkeypatch.setattr(measure, "measure_fleet", fake_fleet)
    r = client.post("/devices/measure", json={"apply": False}, headers=auth)
    assert r.status_code == 200
    assert r.json()["applied"] is False
    assert client.get("/devices", headers=auth).json()[0]["gflops"] == 0.0


def test_single_device_measure_applies_the_solo_figure(client, auth, monkeypatch):
    _add_device(client, auth, "dA")

    async def fake_one(pool, device, **kwargs):
        return measure.Measurement(
            device_id=device.id, ok=True, gflops=800.0, latency_ms=4.0,
            bandwidth_solo_mb_s=55.0,
            sources={"bandwidth": "sftp-blob"},
        )

    monkeypatch.setattr(measure, "measure_device", fake_one)
    r = client.post("/devices/dA/measure", json={}, headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["bandwidth_solo_mb_s"] == 55.0
    assert r.json()["bandwidth_shared_mb_s"] is None

    stored = client.get("/devices", headers=auth).json()[0]
    assert stored["bandwidth_mb_s"] == 55.0
    assert stored["probe_info"]["bandwidth_basis"] == "solo"


def test_single_device_measure_404s_on_an_unknown_id(client, auth):
    assert client.post("/devices/nope/measure", json={}, headers=auth).status_code == 404


def test_measure_routes_are_declared_before_the_wildcard(client):
    """`/devices/measure` must not be swallowed by `/devices/{device_id}`."""
    paths = client.get("/openapi.json").json()["paths"]
    assert "post" in paths["/devices/measure"]
    assert "post" in paths["/devices/{device_id}/measure"]


# ------------------------------------------------- compute contention (real bug)
def test_the_benchmark_is_never_run_on_two_devices_at_once(monkeypatch):
    """Found on real hardware, not by reading the code.

    Nine VMs on one host: benchmarked together each reported ~2.2x less than it
    did alone (63.1 vs 28.7 GFLOPS on the same box), because they were splitting
    one CPU. The figure feeds `cum[cut] / gflops` directly, so the error moves
    the cut layer without anything looking wrong.
    """
    intervals: list[tuple[float, float]] = []

    async def run_command(_conn, cmd, timeout=None):
        # The bench *invocation* only -- the two sha256sum calls also name
        # the script, and they are deliberately outside the lock.
        if ("gflops_bench" in cmd or " -c " in cmd) and "sha256sum" not in cmd:
            start = time.perf_counter()
            await asyncio.sleep(0.05)
            intervals.append((start, time.perf_counter()))
            return measure.cmds.CmdResult(
                device_id="d", command=cmd, exit=0,
                stdout='{"ok": true, "conv_gflops": 100.0, "gemm_gflops": 200.0}',
            )
        return measure.cmds.CmdResult(device_id="d", command=cmd, exit=1, stdout="")

    async def scp_put(_conn, local, remote):
        return 1

    monkeypatch.setattr(measure.cmds, "run_command", run_command)
    monkeypatch.setattr(measure.cmds, "scp_put", scp_put)

    async def scenario():
        conns = [f"conn-{i}" for i in range(5)]
        await asyncio.gather(*(
            measure.measure_compute(c, measure.Measurement(device_id=f"d{i}"))
            for i, c in enumerate(conns)
        ))

    asyncio.run(scenario())

    assert len(intervals) == 5, "the stub caught commands other than the benchmark"
    overlaps = sum(
        1
        for i, (a0, a1) in enumerate(intervals)
        for b0, b1 in intervals[i + 1:]
        if a0 < b1 and b0 < a1
    )
    assert overlaps == 0, "benchmarks overlapped -- shared-host fleets read low"


def test_serialize_compute_false_lets_them_overlap(monkeypatch):
    """The opt-out, for a fleet that really is separate hardware."""
    running = 0
    peak = 0

    async def run_command(_conn, cmd, timeout=None):
        nonlocal running, peak
        # The bench *invocation* only -- the two sha256sum calls also name
        # the script, and they are deliberately outside the lock.
        if ("gflops_bench" in cmd or " -c " in cmd) and "sha256sum" not in cmd:
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.05)
            running -= 1
            return measure.cmds.CmdResult(
                device_id="d", command=cmd, exit=0,
                stdout='{"ok": true, "conv_gflops": 100.0}',
            )
        return measure.cmds.CmdResult(device_id="d", command=cmd, exit=1, stdout="")

    async def scp_put(_conn, local, remote):
        return 1

    monkeypatch.setattr(measure.cmds, "run_command", run_command)
    monkeypatch.setattr(measure.cmds, "scp_put", scp_put)

    async def scenario():
        await asyncio.gather(*(
            measure.measure_compute(
                f"conn-{i}", measure.Measurement(device_id=f"d{i}"), serialize=False
            )
            for i in range(4)
        ))

    asyncio.run(scenario())
    assert peak > 1, "serialize=False still queued them"


# ------------------------------------------------ loopback latency (real bug)
def test_a_loopback_broker_host_is_refused_rather_than_self_pinged():
    """`localhost` on a device means the device.

    An unconfigured DEVICE_BROKER_URL left the target as `localhost`; the TCP
    probe was refused, ICMP then "succeeded" against the device itself, and
    0.06 ms was written to the card as the latency to the broker. A plausible
    number for something never measured is worse than no number.
    """
    calls: list[str] = []

    async def run_command(_conn, cmd, timeout=None):
        calls.append(cmd)
        return measure.cmds.CmdResult(
            device_id="d", command=cmd, exit=0, stdout="", duration_ms=12.5
        )

    import unittest.mock as mock

    m = measure.Measurement(device_id="d")
    with mock.patch.object(measure.cmds, "run_command", run_command):
        asyncio.run(measure.measure_latency("conn", m, "localhost", 5672))

    assert not any("ping" in c for c in calls), "it pinged loopback anyway"
    assert any("means the device itself" in w for w in m.warnings)
    assert any("DEVICE_BROKER_URL" in w for w in m.warnings)
    # And the SSH round trip that follows is a diagnostic, not a spec value.
    assert m.latency_ms is None, "an unmeasurable latency was written anyway"
    assert m.info["ssh_rtt_ms"] == 12.5
    assert m.sources["latency"] == "ssh-rtt (not applied)"


def test_a_routable_broker_host_is_still_probed_normally():
    calls: list[str] = []

    async def run_command(_conn, cmd, timeout=None):
        calls.append(cmd)
        return measure.cmds.CmdResult(
            device_id="d", command=cmd, exit=0,
            stdout='{"ok": true, "min_ms": 2.5, "avg_ms": 2.6, "max_ms": 3.0}',
        )

    import unittest.mock as mock

    m = measure.Measurement(device_id="d")
    with mock.patch.object(measure.cmds, "run_command", run_command):
        asyncio.run(measure.measure_latency("conn", m, "10.0.0.5", 5672))

    assert m.latency_ms == 2.5
    assert m.sources["latency"] == "tcp-connect"
    assert not m.warnings


def test_the_ssh_round_trip_is_never_written_to_the_latency_field():
    """Measured through a jump host it read 390-578 ms for machines 0.6 ms from
    their broker. That is not a rough latency, it is a fictional one."""
    async def run_command(_conn, cmd, timeout=None):
        return measure.cmds.CmdResult(
            device_id="d", command=cmd, exit=1, stdout="", duration_ms=453.0
        )

    import unittest.mock as mock

    device = Device(id="d", name="d", latency_ms=6.0)
    m = measure.Measurement(device_id="d", ok=True, gflops=63.1)
    with mock.patch.object(measure.cmds, "run_command", run_command):
        asyncio.run(measure.measure_latency("conn", m, "10.0.0.5", 5672))

    measure.apply_to_device(device, m, "solo")
    assert device.latency_ms == 6.0, "the operator's own figure was overwritten"
    assert device.gflops == 63.1, "the measured GFLOPS should still be applied"
    assert device.probe_info["ssh_rtt_ms"] == 453.0


# ------------------------------------------- bandwidth behind a jump host (real)
def test_bandwidth_is_skipped_when_the_devices_are_behind_a_jump_host(monkeypatch):
    """Measured for real: a 16 MB pull took 6m53s over the operator's tunnel,
    for machines 0.6 ms from their broker. The number describes the wrong link
    and the wait would hang the UI button for an hour."""
    async def load_config():
        class Cfg:
            jump_enabled = True
            host = "100.68.127.89"
            ssh_username = "dai"
        return Cfg()

    from app.ssh import gateway
    monkeypatch.setattr(gateway, "load_config", load_config)

    pulled = []

    async def pull(conn, path):
        pulled.append(path)
        return 1.0, 16_000_000

    monkeypatch.setattr(measure, "_pull_blob", pull)

    m = measure.Measurement(device_id="d")
    asyncio.run(measure.measure_bandwidth("conn", m))

    assert pulled == [], "it transferred over the tunnel anyway"
    assert m.bandwidth_solo_mb_s is None
    assert m.sources["bandwidth"] == "skipped (jump host)"
    assert any("jump host" in w for w in m.warnings)


def test_an_explicit_iperf_server_is_measured_even_behind_a_jump_host(monkeypatch):
    """iperf runs device -> the named server, which was never this console."""
    async def load_config():
        class Cfg:
            jump_enabled = True
            host = "100.68.127.89"
            ssh_username = "dai"
        return Cfg()

    from app.ssh import gateway
    monkeypatch.setattr(gateway, "load_config", load_config)

    async def run_command(_conn, cmd, timeout=None):
        payload = '{"end": {"sum_sent": {"bits_per_second": 800000000}}}'
        return measure.cmds.CmdResult(device_id="d", command=cmd, exit=0, stdout=payload)

    monkeypatch.setattr(measure.cmds, "run_command", run_command)

    m = measure.Measurement(device_id="d")
    asyncio.run(measure.measure_bandwidth("conn", m, iperf_server="10.0.0.5"))

    assert m.bandwidth_solo_mb_s == 100.0
    assert m.sources["bandwidth"] == "iperf3"


def test_a_direct_fleet_still_measures_bandwidth(monkeypatch):
    """No jump host configured -> this console is on the devices' network."""
    async def load_config():
        return None

    from app.ssh import gateway
    monkeypatch.setattr(gateway, "load_config", load_config)

    async def noop(*args, **kwargs):
        return ""

    async def pull(conn, path):
        return 1.0, 16_000_000

    monkeypatch.setattr(measure, "_make_blob", noop)
    monkeypatch.setattr(measure, "_remove_blob", noop)
    monkeypatch.setattr(measure, "_pull_blob", pull)

    m = measure.Measurement(device_id="d")
    asyncio.run(measure.measure_bandwidth("conn", m))
    assert m.bandwidth_solo_mb_s == 16.0
    assert m.sources["bandwidth"] == "sftp-blob"


# ---------------------------------------------- contention needs a real overlap
def test_the_contention_pass_gives_every_device_the_same_start(monkeypatch):
    """Found on real hardware: three co-located VMs measured 11.5 MB/s each
    alone, then 8.84 / 4.83 / 4.79 together -- not because they differ, but
    because whoever connected first published part of its data with the broker
    to itself. With an agreed start they read 3.94 / 3.97 / 3.93."""
    starts: list[float] = []

    async def amqp(conn, m, *, start_at=0.0):
        starts.append(start_at)
        return {"mb_s": 4.0, "start_skew_ms": 0.0}

    monkeypatch.setattr(measure, "_amqp_bandwidth", amqp)

    group = [
        measure.Measurement(device_id=f"d{i}", ok=True, bandwidth_solo_mb_s=11.5,
                            sources={"bandwidth": "amqp-publish"})
        for i in range(3)
    ]
    devices = {m.device_id: Device(id=m.device_id, name=m.device_id) for m in group}

    asyncio.run(measure._contend_amqp(FakePool(), devices, group))

    assert len(starts) == 3
    assert len(set(starts)) == 1, "each device was given a different start time"
    assert starts[0] > 0, "no barrier was set at all"
    assert all(m.bandwidth_shared_mb_s == 4.0 for m in group)


def test_a_device_that_missed_the_start_says_so(monkeypatch):
    """A skewed clock makes the shared figure worth less, and silently."""
    async def amqp(conn, m, *, start_at=0.0):
        skew = 4000.0 if m.device_id == "d1" else 0.0
        return {"mb_s": 9.0, "start_skew_ms": skew}

    monkeypatch.setattr(measure, "_amqp_bandwidth", amqp)

    group = [
        measure.Measurement(device_id=f"d{i}", ok=True, bandwidth_solo_mb_s=11.5,
                            sources={"bandwidth": "amqp-publish"})
        for i in range(2)
    ]
    devices = {m.device_id: Device(id=m.device_id, name=m.device_id) for m in group}
    asyncio.run(measure._contend_amqp(FakePool(), devices, group))

    late = next(m for m in group if m.device_id == "d1")
    ontime = next(m for m in group if m.device_id == "d0")
    assert any("off the agreed start" in w for w in late.warnings)
    assert not any("off the agreed start" in w for w in ontime.warnings)
