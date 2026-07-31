#!/usr/bin/env python3
"""One-way latency from this device to the RabbitMQ broker, in milliseconds.

Pushed to every device by `app/services/measure.py` and run there; prints one
JSON line on stdout and nothing else.

    SPLITINF_AMQP_URL='amqp://user:pw@host:5672/' python3 amqp_rtt.py --tries 10

How
---
Publish a small message to a queue this device is itself consuming, and time
how long it takes to come back. That round trip is device -> broker -> device,
so **half of it** is the one-way delay the pipeline pays when an edge publishes
a feature map: `latency_ms` in the simulator is a per-hop delay, not a round
trip.

Why not just time a TCP handshake
---------------------------------
A handshake measures the network and stops there. This measures what a message
actually costs, which is the network *plus* everything RabbitMQ does with it --
routing, queue accounting, dispatch back to a consumer. On a fast LAN that
broker-side work is not a rounding error: it is most of the number.

It is also the same connection, the same exchange and the same publish call the
edge agent makes, so a change that slows the broker down shows up here.

The consumer is opened **once**, before the loop. Opening it per message, or
polling with `basic_get`, would put a second round trip inside the thing being
timed and roughly double every reading.

The queue is exclusive and auto-delete: it exists for the length of the
connection and takes its messages with it. Messages are transient
(`delivery_mode=1`) so the figure is the link and the broker, not the disk.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
import uuid


def measure(url: str, tries: int, payload_bytes: int) -> dict:
    import pika

    queue = "splitinf_rtt_%s" % uuid.uuid4().hex[:10]
    body = b"x" * max(1, payload_bytes)

    params = pika.URLParameters(url)
    params.heartbeat = 30
    params.blocked_connection_timeout = 30
    params.socket_timeout = 15

    connect_start = time.perf_counter()
    conn = pika.BlockingConnection(params)
    connect_ms = (time.perf_counter() - connect_start) * 1000.0

    round_trips: list[float] = []
    timeouts = 0
    try:
        channel = conn.channel()
        channel.queue_declare(queue=queue, exclusive=True, auto_delete=True, durable=False)
        props = pika.BasicProperties(delivery_mode=1)

        # Opened once: a consumer set up inside the loop would be timed too.
        consumer = channel.consume(queue, auto_ack=True, inactivity_timeout=5.0)

        # One untimed warm-up. The first message pays for consumer registration
        # and TCP slow start, neither of which the pipeline pays per frame.
        channel.basic_publish(exchange="", routing_key=queue, body=body, properties=props)
        next(consumer, None)

        for _ in range(tries):
            start = time.perf_counter()
            channel.basic_publish(
                exchange="", routing_key=queue, body=body, properties=props
            )
            received = next(consumer, None)
            if received is None or received[0] is None:
                timeouts += 1
                continue
            round_trips.append((time.perf_counter() - start) * 1000.0)

        channel.cancel()
    finally:
        try:
            conn.close()                     # takes the queue with it
        except Exception:                    # noqa: BLE001
            pass

    if not round_trips:
        return {"ok": False, "error": "no message came back (%d timeouts)" % timeouts}

    ordered = sorted(round_trips)
    mean = sum(round_trips) / len(round_trips)
    var = sum((v - mean) ** 2 for v in round_trips) / len(round_trips)
    return {
        "ok": True,
        # Halved: the message went there and back, the pipeline only goes there.
        #
        # The **mean**, not the minimum. The minimum is what the link can do on
        # a good day; the simulator adds this delay to every frame, so what it
        # needs is what a frame costs on an average day. The minimum is kept
        # below for anyone sizing the network rather than predicting a run.
        "one_way_ms": round(mean / 2.0, 3),
        "one_way_min_ms": round(ordered[0] / 2.0, 3),
        "one_way_std_ms": round(var ** 0.5 / 2.0, 3),
        "rtt_min_ms": round(ordered[0], 3),
        "rtt_avg_ms": round(mean, 3),
        "rtt_max_ms": round(ordered[-1], 3),
        "samples": len(round_trips),
        "timeouts": timeouts,
        "payload_bytes": len(body),
        "connect_ms": round(connect_ms, 2),
        "broker": "%s:%s" % (params.host, params.port),
        "host": platform.node(),
        "pika": pika.__version__,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tries", type=int, default=10,
                    help="timed round trips (default: 10)")
    ap.add_argument("--bytes", type=int, default=64,
                    help="payload size; small on purpose, this is a delay not a "
                         "throughput test (default: 64)")
    args = ap.parse_args()

    url = os.environ.get("SPLITINF_AMQP_URL", "").strip()
    if not url:
        print(json.dumps({"ok": False, "error": "SPLITINF_AMQP_URL is not set"}))
        return 0

    try:
        result = measure(url, max(1, args.tries), max(1, args.bytes))
    except Exception as exc:                                  # noqa: BLE001
        result = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}

    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
