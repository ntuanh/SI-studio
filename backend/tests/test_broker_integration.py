"""Live RabbitMQ integration: agent reports -> aggregation -> §6 payload.

Skipped automatically when no broker is reachable, so the default `pytest` run
stays hermetic. Point BROKER_URL at a real instance (or `docker compose up
rabbitmq`) to exercise these.

Note on structure: an aio-pika connection is bound to the event loop that
created it, so every test opens *and* uses its broker inside a single
`asyncio.run` via the `run_with_broker` helper. Opening the connection in a
fixture's loop and using it in the test's loop deadlocks.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import pytest

from app.config import settings
from app.inference import broker as broker_mod
from app.inference.broker import METRICS_QUEUE, Broker, intermediate_queue
from app.inference.orchestrator import ActiveRun, _Window, orchestrator
from app.services.metrics_bus import bus

T = TypeVar("T")

CONNECT_TIMEOUT = 8.0


@pytest.fixture(autouse=True)
def fresh_global_broker():
    """Drop the orchestrator's shared broker handles between tests.

    `orchestrator._ensure_collector()` consumes through the module-level broker
    singleton. Each test here runs in its own `asyncio.run` loop, and a
    connection from a previous (now-closed) loop still reports itself as open --
    so reusing it would hang instead of reconnecting. Production has a single
    long-lived loop and never hits this.
    """
    b = broker_mod.broker
    b._conn = None
    b._channel = None
    b._queues.clear()
    b._consumer_tags.clear()
    b._lock = asyncio.Lock()
    orchestrator._consuming = False
    orchestrator._broadcast_task = None
    yield


def run_with_broker(scenario: Callable[[Broker], Awaitable[T]]) -> T:
    """Open a broker, run `scenario`, always close. Skips if unreachable."""

    async def main() -> T:
        b = Broker(settings.broker_url)
        try:
            await asyncio.wait_for(b.connect(), timeout=CONNECT_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - any failure means "no broker"
            await b.close()
            pytest.skip(f"no usable RabbitMQ at {settings.broker_url}: {exc}")
        try:
            return await asyncio.wait_for(scenario(b), timeout=60)
        finally:
            await b.close()

    return asyncio.run(main())


async def _wait_for(predicate: Callable[[], bool], timeout: float = 10.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return predicate()


# --------------------------------------------------------------- broker basics
def test_queue_declare_publish_consume_round_trip():
    queue = f"test_intermediate_{uuid.uuid4().hex[:8]}"
    received: list[dict[str, Any]] = []

    async def scenario(b: Broker) -> int:
        await b.declare(queue)

        async def handler(data: dict[str, Any]) -> None:
            received.append(data)

        await b.consume(queue, handler)
        for i in range(5):
            await b.publish(queue, {"frame_id": i, "cluster": 1})

        await _wait_for(lambda: len(received) >= 5)
        await b.cancel_consumer(queue)
        depth = await b.queue_depth(queue)
        await b.purge(queue)
        return depth

    depth = run_with_broker(scenario)
    assert len(received) == 5
    assert sorted(r["frame_id"] for r in received) == [0, 1, 2, 3, 4]
    assert depth == 0  # everything was consumed and acked


def test_malformed_messages_do_not_kill_the_consumer():
    queue = f"test_malformed_{uuid.uuid4().hex[:8]}"
    good: list[dict[str, Any]] = []

    async def scenario(b: Broker) -> None:
        await b.declare(queue)

        async def handler(data: dict[str, Any]) -> None:
            good.append(data)

        await b.consume(queue, handler)
        # Not JSON, then JSON that isn't an object, then a valid report.
        await b.publish(queue, b"\x00 not json at all")
        await b.publish(queue, b"[1, 2, 3]")
        await b.publish(queue, {"frame_id": 99})

        await _wait_for(lambda: bool(good))
        await b.cancel_consumer(queue)
        await b.purge(queue)

    run_with_broker(scenario)
    assert [g["frame_id"] for g in good] == [99]


def test_queue_stats_reports_depth():
    queue = f"test_depth_{uuid.uuid4().hex[:8]}"

    async def scenario(b: Broker) -> tuple[dict[str, Any], int, int]:
        await b.declare(queue)
        for i in range(7):
            await b.publish(queue, {"i": i})
        # No sleep needed: a passive declare sees published messages at once.
        # (The management API's messages_ready would still read 0 here -- it
        # only refreshes on RabbitMQ's statistics interval.)
        depth = await b.queue_depth(queue)
        stats = await b.stats(queue)
        purged = await b.purge(queue)
        return stats, depth, purged

    stats, depth, purged = run_with_broker(scenario)
    assert depth == 7
    assert stats["queue_depth"] == 7  # must not come from the lagging mgmt API
    assert purged == 7


def test_cluster_queue_declaration_uses_the_ui_naming():
    async def scenario(b: Broker) -> dict[str, str]:
        names = await b.declare_cluster_queues(3)
        depth = await b.queue_depth(names["intermediate"])
        assert depth == 0
        return names

    names = run_with_broker(scenario)
    assert names["intermediate"] == "intermediate_queue_3"
    assert names["metrics"] == "metrics_queue"
    assert names["fps"] == "fps_queue"


def test_probe_broker_reads_the_real_server_version():
    """Acceptance (backend_update.md): /server/test returns a real version.

    Guards the `server_properties` lookup, which lives on the innermost aiormq
    connection (`conn.transport.connection` in aio-pika 9.x) -- an earlier
    attribute path returned ok=True with an empty version string.
    """
    import re

    from app.inference.broker import probe_broker

    async def scenario(_b: Broker) -> dict[str, Any]:
        return await probe_broker(settings.broker_url)

    result = run_with_broker(scenario)
    assert result["ok"] is True
    assert result["error"] == ""
    assert re.match(r"^\d+\.\d+", result["rabbitmq_version"]), result
    assert result["product"]


# ------------------------------------------------------- §6.4 metric collection
def test_agent_reports_become_a_live_metrics_payload():
    """Cloud-agent reports on metrics_queue must aggregate into the §6 shape."""
    cluster_id = 991
    queue = intermediate_queue(cluster_id)

    orchestrator._runs[cluster_id] = ActiveRun(
        run_id="itest", cluster_id=cluster_id, queue_name=queue, cut=6,
        model_name="yolov11n", num_bit=8, batch_size=32,
        edge_ids=["dA", "dB"], cloud_ids=["dG1"], started_at=time.monotonic(),
    )
    orchestrator._windows[cluster_id] = _Window(cut=6)

    async def scenario(b: Broker) -> dict[str, Any] | None:
        await orchestrator._ensure_collector()
        try:
            now = time.time()
            for i in range(20):
                await b.publish(
                    METRICS_QUEUE,
                    {
                        "cluster": cluster_id,
                        "run_id": "itest",
                        "frame_id": i,
                        "cut": 6,
                        "device_id": "dG1",
                        "edge_device_id": "dA" if i % 2 else "dB",
                        "edge_ms": 12.0,
                        "transfer_ms": 30.0,
                        "cloud_ms": 4.0,
                        "e2e_ms": 46.0,
                        "msg_mb": 0.19,
                        "ts": now + i * 0.05,
                    },
                )

            window = orchestrator._windows[cluster_id]
            await _wait_for(lambda: window.frames >= 20, timeout=20)
            return await orchestrator.live_payload(cluster_id)
        finally:
            await orchestrator._teardown_collector()
            await b.purge(METRICS_QUEUE)

    try:
        payload = run_with_broker(scenario)

        assert payload is not None, "collector never received the reports"
        assert payload["cluster"] == cluster_id
        assert payload["cut"] == 6
        assert payload["source"] == "live"
        assert payload["frames"] == 20

        # Averages of the constant inputs.
        assert payload["edge_ms"] == pytest.approx(12.0, abs=1e-6)
        assert payload["transfer_ms"] == pytest.approx(30.0, abs=1e-6)
        assert payload["cloud_ms"] == pytest.approx(4.0, abs=1e-6)
        assert payload["e2e_ms"] == pytest.approx(46.0, abs=1e-6)
        assert payload["msg_mb"] == pytest.approx(0.19, abs=1e-6)

        # transfer is the bottleneck (30 > 12 > 4), so utils are ratios of it.
        assert payload["transfer_util"] == 1.0
        assert payload["edge_util"] == pytest.approx(12 / 30, abs=1e-4)
        assert payload["cloud_util"] == pytest.approx(4 / 30, abs=1e-4)

        # Per-device entries carry roles and stay in [0, 1].
        by_id = {d["id"]: d for d in payload["devices"]}
        assert set(by_id) == {"dA", "dB", "dG1"}
        assert by_id["dA"]["role"] == "head" and by_id["dG1"]["role"] == "tail"
        assert by_id["dA"]["util"] == pytest.approx(12 / 30, abs=1e-4)
        assert all(0.0 <= d["util"] <= 1.0 for d in payload["devices"])

        # Every key the UI reads is present.
        for key in (
            "cluster", "cut", "edge_ms", "transfer_ms", "cloud_ms", "e2e_ms",
            "msg_mb", "fps", "edge_util", "transfer_util", "cloud_util",
            "queue_depth", "devices",
        ):
            assert key in payload, key
    finally:
        orchestrator._runs.pop(cluster_id, None)
        orchestrator._windows.pop(cluster_id, None)
        bus.clear_metrics(cluster_id)


def test_reports_for_a_stopped_run_are_ignored():
    """Late reports must not resurrect a window or crash the collector."""
    cluster_id = 992

    async def scenario(b: Broker) -> dict[str, Any] | None:
        await orchestrator._ensure_collector()
        try:
            await b.publish(
                METRICS_QUEUE,
                {"cluster": cluster_id, "edge_ms": 1.0, "transfer_ms": 1.0, "cloud_ms": 1.0},
            )
            await asyncio.sleep(1.0)
            return await orchestrator.live_payload(cluster_id)
        finally:
            await orchestrator._teardown_collector()
            await b.purge(METRICS_QUEUE)

    assert run_with_broker(scenario) is None
    assert cluster_id not in orchestrator._windows


def test_fps_is_derived_from_arrival_times():
    """fps comes from wall-clock completions, not from the reported timings."""
    cluster_id = 993
    orchestrator._runs[cluster_id] = ActiveRun(
        run_id="fpstest", cluster_id=cluster_id, queue_name=intermediate_queue(cluster_id),
        cut=4, model_name="yolov11n", num_bit=8, batch_size=1,
        edge_ids=["dA"], cloud_ids=["dG1"], started_at=time.monotonic(),
    )
    win = _Window(cut=4)
    orchestrator._windows[cluster_id] = win

    async def scenario(_b: Broker) -> dict[str, Any] | None:
        # Feed the window directly at a known cadence: 10 frames, 20ms apart.
        base = time.monotonic()
        for i in range(10):
            win.edge_ms.append(5.0)
            win.transfer_ms.append(10.0)
            win.cloud_ms.append(2.0)
            win.e2e_ms.append(17.0)
            win.msg_mb.append(0.1)
            win.completions.append(base + i * 0.02)
            win.frames += 1
        win.last_report = time.monotonic()
        return await orchestrator.live_payload(cluster_id)

    try:
        payload = run_with_broker(scenario)
        assert payload is not None
        # 9 intervals over 0.18s -> 50 fps, independent of the 17ms e2e.
        assert payload["fps"] == pytest.approx(50.0, rel=0.02)
        assert payload["e2e_ms"] == pytest.approx(17.0, abs=1e-6)
    finally:
        orchestrator._runs.pop(cluster_id, None)
        orchestrator._windows.pop(cluster_id, None)
        bus.clear_metrics(cluster_id)
