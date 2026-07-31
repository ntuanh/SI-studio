"""RabbitMQ access for the control plane (guide §6.2).

Queues
------
    intermediate_queue_<cluster>   edge -> cloud feature maps (durable)
    fps_queue                      per-cluster throughput reports
    metrics_queue                  per-frame timing reports consumed here

The agents on the devices use blocking `pika`; this side is `aio-pika`.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

import aio_pika
from aio_pika.abc import AbstractIncomingMessage, AbstractQueue, AbstractRobustConnection

from ..config import settings

log = logging.getLogger(__name__)

METRICS_QUEUE = "metrics_queue"
FPS_QUEUE = "fps_queue"


def intermediate_queue(cluster_id: int) -> str:
    return f"intermediate_queue_{cluster_id}"


class BrokerUnavailable(RuntimeError):
    """RabbitMQ could not be reached. Surfaced to the UI as a 503."""


class Broker:
    """Robust connection + a channel, with queue declaration and consumers."""

    def __init__(self, url: str | None = None) -> None:
        self._url = url or settings.broker_url
        self._conn: AbstractRobustConnection | None = None
        self._channel: aio_pika.abc.AbstractRobustChannel | None = None
        self._queues: dict[str, AbstractQueue] = {}
        self._consumer_tags: dict[str, str] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------- connection
    @property
    def connected(self) -> bool:
        return self._conn is not None and not self._conn.is_closed

    async def connect(self) -> None:
        async with self._lock:
            if self.connected:
                return
            try:
                self._conn = await aio_pika.connect_robust(self._url, timeout=10)
            except (OSError, asyncio.TimeoutError, aio_pika.exceptions.AMQPError) as exc:
                self._conn = None
                raise BrokerUnavailable(
                    f"cannot reach RabbitMQ at {_redact(self._url)}: {exc}"
                ) from exc
            self._channel = await self._conn.channel(publisher_confirms=True)
            await self._channel.set_qos(prefetch_count=200)
            log.info("broker connected: %s", _redact(self._url))

    async def close(self) -> None:
        async with self._lock:
            self._queues.clear()
            self._consumer_tags.clear()
            if self._conn is not None and not self._conn.is_closed:
                await self._conn.close()
            self._conn, self._channel = None, None

    async def _ch(self) -> aio_pika.abc.AbstractRobustChannel:
        if not self.connected or self._channel is None:
            await self.connect()
        if self._channel is None:
            raise BrokerUnavailable(
                f"no RabbitMQ channel: {_redact(self._url)} is unreachable"
            )
        return self._channel

    # ------------------------------------------------------------ declaration
    async def declare(self, name: str, *, durable: bool = True) -> AbstractQueue:
        if (q := self._queues.get(name)) is not None:
            return q
        ch = await self._ch()
        q = await ch.declare_queue(name, durable=durable)
        self._queues[name] = q
        return q

    async def declare_cluster_queues(self, cluster_id: int) -> dict[str, str]:
        """Declare everything one cluster's run needs."""
        names = {
            "intermediate": intermediate_queue(cluster_id),
            "metrics": METRICS_QUEUE,
            "fps": FPS_QUEUE,
        }
        for n in names.values():
            await self.declare(n)
        return names

    # ------------------------------------------------------------- publishing
    async def publish(self, queue_name: str, payload: dict[str, Any] | bytes) -> None:
        ch = await self._ch()
        await self.declare(queue_name)
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        await ch.default_exchange.publish(
            aio_pika.Message(body=body, delivery_mode=aio_pika.DeliveryMode.PERSISTENT),
            routing_key=queue_name,
        )

    # -------------------------------------------------------------- consuming
    async def consume(
        self,
        queue_name: str,
        handler: Callable[[dict[str, Any]], Awaitable[None]],
        *,
        durable: bool = True,
    ) -> str:
        """Attach a JSON consumer. Malformed bodies are acked and dropped."""
        q = await self.declare(queue_name, durable=durable)

        async def on_message(message: AbstractIncomingMessage) -> None:
            async with message.process(requeue=False):
                try:
                    data = json.loads(message.body.decode("utf-8", "replace"))
                except json.JSONDecodeError:
                    log.warning("dropping non-JSON message on %s", queue_name)
                    return
                if not isinstance(data, dict):
                    return
                try:
                    await handler(data)
                except Exception:  # a bad frame must not kill the consumer
                    log.exception("metrics handler failed on %s", queue_name)

        tag = await q.consume(on_message)
        self._consumer_tags[queue_name] = tag
        log.info("consuming %s (tag=%s)", queue_name, tag)
        return tag

    async def cancel_consumer(self, queue_name: str) -> None:
        tag = self._consumer_tags.pop(queue_name, None)
        q = self._queues.get(queue_name)
        if tag and q is not None:
            await q.cancel(tag)

    # -------------------------------------------------------------- inspection
    async def queue_depth(self, queue_name: str) -> int:
        """Ready-message count. Uses a passive declare on its own channel so a
        missing queue doesn't kill the shared one."""
        try:
            conn = self._conn
            if conn is None or conn.is_closed:
                await self.connect()
                conn = self._conn
            assert conn is not None
            async with conn.channel() as ch:
                q = await ch.declare_queue(queue_name, passive=True)
                return int(q.declaration_result.message_count or 0)
        except Exception:
            return 0

    async def purge(self, queue_name: str) -> int:
        """Drain a queue (used by /run/stop). Returns 0 if it doesn't exist."""
        try:
            q = await self.declare(queue_name)
            result = await q.purge()
            return int(getattr(result, "message_count", 0) or 0)
        except Exception:
            log.debug("purge failed for %s", queue_name, exc_info=True)
            return 0

    async def stats(self, queue_name: str) -> dict[str, Any]:
        """Queue depth plus, when the management API is configured, consumer
        count and ack rate.

        `queue_depth` always comes from a passive declare, which is immediate.
        The management API's `messages_ready` is only refreshed on RabbitMQ's
        statistics interval (~5s by default), so using it here would make the
        UI's queue-depth gauge lag behind reality. The management API is used
        solely for the fields a declare cannot provide.
        """
        out: dict[str, Any] = {"queue_depth": await self.queue_depth(queue_name)}

        mgmt = (settings.rabbitmq_mgmt_url or "").strip().rstrip("/")
        if mgmt:
            data = await self._mgmt_queue(mgmt, queue_name)
            if data is not None:
                rate = ((data.get("message_stats") or {}).get("ack_details") or {}).get("rate")
                out["unacked"] = int(data.get("messages_unacknowledged") or 0)
                out["consumers"] = int(data.get("consumers") or 0)
                out["ack_rate"] = float(rate) if rate is not None else None
        return out

    async def _mgmt_queue(self, mgmt: str, queue_name: str) -> dict[str, Any] | None:
        """GET /api/queues/<vhost>/<name> without pulling in an HTTP client."""
        from urllib.error import URLError
        from urllib.request import Request, urlopen

        vhost = quote(settings.rabbitmq_vhost or "/", safe="")
        url = f"{mgmt}/api/queues/{vhost}/{quote(queue_name, safe='')}"
        token = base64.b64encode(
            f"{settings.rabbitmq_mgmt_user}:{settings.rabbitmq_mgmt_password}".encode()
        ).decode()
        req = Request(url, headers={"Authorization": f"Basic {token}"})

        def _fetch() -> dict[str, Any] | None:
            try:
                with urlopen(req, timeout=3) as resp:  # noqa: S310 - fixed scheme from config
                    return json.loads(resp.read().decode())
            except (URLError, OSError, json.JSONDecodeError, ValueError):
                return None

        return await asyncio.to_thread(_fetch)


async def probe_broker(url: str, *, timeout: float = 8.0) -> dict[str, Any]:
    """Open a throwaway AMQP connection and read the server's identity.

    Used by POST /server/test. Returns
    `{ok, rabbitmq_version, product, error}` and never raises.
    """
    out: dict[str, Any] = {"ok": False, "rabbitmq_version": "", "product": "", "error": ""}
    conn = None
    try:
        conn = await asyncio.wait_for(aio_pika.connect(url, timeout=timeout), timeout=timeout + 2)
        props = _server_properties(conn)
        out["rabbitmq_version"] = _as_text(props.get("version"))
        out["product"] = _as_text(props.get("product"))
        out["cluster_name"] = _as_text(props.get("cluster_name"))
        out["platform"] = _as_text(props.get("platform"))
        out["ok"] = True
    except (OSError, asyncio.TimeoutError, aio_pika.exceptions.AMQPError) as exc:
        out["error"] = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    except Exception as exc:  # noqa: BLE001 - a probe must never propagate
        out["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if conn is not None:
            with contextlib.suppress(Exception):
                await conn.close()
    return out


def _server_properties(conn: Any) -> dict[str, Any]:
    """Dig out the broker's identity dict from an open aio-pika connection.

    aio-pika wraps aiormq, and the handshake properties sit on the innermost
    aiormq connection. In 9.x that is `conn.transport.connection`; older and
    robust variants expose `conn.connection` or the attribute directly. Try each
    rather than pinning one layout, and tolerate none of them being present --
    the version string is informational, so a miss must not fail the probe.
    """
    candidates = (
        getattr(getattr(conn, "transport", None), "connection", None),
        getattr(conn, "connection", None),
        conn,
    )
    for holder in candidates:
        props = getattr(holder, "server_properties", None)
        if props:
            try:
                return dict(props)
            except (TypeError, ValueError):
                continue
    return {}


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _redact(url: str) -> str:
    if "@" in url and "//" in url:
        scheme, rest = url.split("//", 1)
        return f"{scheme}//***@{rest.split('@', 1)[1]}"
    return url


#: Process-wide singleton, wired into the app lifespan.
broker = Broker()
