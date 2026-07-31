"""In-process pub/sub. Producers (SSH pool, orchestrator) publish envelopes;
the /ws/stream endpoint fans them out to browsers.

Envelope shapes (guide §7):
    {"type": "ssh_status", "device_id": "d1", "status": "on"}
    {"type": "exec_line",  "device_id": "d1", "text": "31% util", "stream": "stdout"}
    {"type": "metrics",    "payload": {...§6...}}
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

#: Per-subscriber backlog. A browser that stalls drops frames rather than
#: applying backpressure to the orchestrator.
SUBSCRIBER_QUEUE_SIZE = 256


class MetricsBus:
    def __init__(self, history: int = 200) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._history: deque[dict[str, Any]] = deque(maxlen=history)
        #: Latest metrics payload per cluster -- served by GET /metrics/latest.
        self._latest_metrics: dict[int, dict[str, Any]] = {}
        #: Latest known SSH status per device -- replayed to new subscribers.
        self._ssh_status: dict[str, str] = {}
        #: Latest broker/control-API reachability -- also replayed.
        self._server_status: str = "off"

    # ------------------------------------------------------------- publishing
    def publish(self, message: dict[str, Any]) -> None:
        """Non-blocking broadcast. Safe to call from any running-loop context."""
        message.setdefault("ts", datetime.now(timezone.utc).isoformat())

        mtype = message.get("type")
        if mtype == "metrics":
            payload = message.get("payload") or {}
            cid = payload.get("cluster")
            if cid is not None:
                self._latest_metrics[int(cid)] = payload
        elif mtype == "ssh_status":
            did = message.get("device_id")
            if did:
                self._ssh_status[str(did)] = str(message.get("status", "off"))

        if mtype != "metrics":  # metrics are high-volume; keep the log readable
            self._history.append(message)

        dead: list[asyncio.Queue[dict[str, Any]]] = []
        for q in self._subscribers:
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                # Drop the oldest frame and retry once.
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
                try:
                    q.put_nowait(message)
                except asyncio.QueueFull:
                    dead.append(q)
        for q in dead:
            self._subscribers.discard(q)

    # --- typed conveniences -------------------------------------------------
    def ssh_status(self, device_id: str, status: str, detail: str = "") -> None:
        msg: dict[str, Any] = {"type": "ssh_status", "device_id": device_id, "status": status}
        if detail:
            msg["detail"] = detail
        self.publish(msg)

    def exec_line(self, device_id: str, text: str, stream: str = "stdout") -> None:
        self.publish({"type": "exec_line", "device_id": device_id, "text": text, "stream": stream})

    def server_status(self, status: str, **fields: Any) -> None:
        """Broker/control-API reachability, driving the Control tab's dot."""
        self._server_status = status
        self.publish({"type": "server_status", "status": status, **fields})

    def metrics(self, payload: dict[str, Any]) -> None:
        self.publish({"type": "metrics", "payload": payload})

    def event(self, name: str, **fields: Any) -> None:
        """Generic control-plane notice (deploy started, run stopped, ...)."""
        self.publish({"type": "event", "name": name, **fields})

    # ------------------------------------------------------------ subscribing
    @contextlib.asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        self._subscribers.add(q)
        try:
            yield q
        finally:
            self._subscribers.discard(q)

    # ----------------------------------------------------------------- state
    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def snapshot(self) -> dict[str, Any]:
        """Replayed to a client right after it connects, so the dots and the
        console are populated without waiting for the next event."""
        return {
            "type": "snapshot",
            "ssh_status": dict(self._ssh_status),
            "server_status": self._server_status,
            "metrics": list(self._latest_metrics.values()),
            "recent": list(self._history)[-50:],
        }

    def latest_metrics(self) -> list[dict[str, Any]]:
        return [self._latest_metrics[k] for k in sorted(self._latest_metrics)]

    def latest_for_cluster(self, cluster_id: int) -> dict[str, Any] | None:
        return self._latest_metrics.get(cluster_id)

    def known_status(self, device_id: str) -> str:
        return self._ssh_status.get(device_id, "off")

    def clear_metrics(self, cluster_id: int | None = None) -> None:
        if cluster_id is None:
            self._latest_metrics.clear()
        else:
            self._latest_metrics.pop(cluster_id, None)


#: Process-wide singleton.
bus = MetricsBus()
