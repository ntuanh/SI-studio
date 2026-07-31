#!/usr/bin/env python3
"""Throughput from this device to the RabbitMQ broker, in MB/s.

Pushed to every device by `app/services/measure.py` and run there; prints one
JSON line on stdout and nothing else.

    SPLITINF_AMQP_URL='amqp://user:pw@host:5672/' python3 amqp_bw.py --mb 32

Why publish to the broker instead of running a transfer test
------------------------------------------------------------
Because that is the path the pipeline uses. The edge agent's whole job is to
publish an encoded feature map to `intermediate_queue_<n>`, so the number that
belongs in `bandwidth_mb_s` is how fast *this device* can push bytes into *that
broker* -- including RabbitMQ's own ingestion cost, which a raw socket
benchmark would leave out.

It also needs nothing installed: no iperf server, no listening port, no second
machine. Every device that can run the agent can already run this, because it
is the same `pika` connection the agent makes.

The alternative -- pulling a file back to the control console over SSH --
measures the operator's link to the device, which behind a jump host is not
even close to the same network. Measured on a real fleet it reported 0.04 MB/s
for machines sitting 0.5 ms from their broker.

The credential arrives in the environment rather than in `argv`, because
`/proc/<pid>/cmdline` is world-readable and `/proc/<pid>/environ` is not.

What is sent
------------
`--mb` megabytes of incompressible bytes (`os.urandom`), split into `--msg-mb`
messages, published with **publisher confirms** on so each one is timed to the
broker's acknowledgement rather than to a local buffer.

The queue is exclusive and auto-delete, so it exists only for the length of the
connection and takes its messages with it when it goes. They are published
transient (`delivery_mode=1`): the point is to time the link and the broker's
ingest, not the disk underneath it.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
import uuid

#: Longest a device will wait for an agreed start before giving up on it.
MAX_START_WAIT_S = 60.0


def _stats(values):
    """Mean, spread and extremes across repeated rounds."""
    n = len(values)
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / n if n > 1 else 0.0
    return {
        "mean": round(mean, 2), "std": round(var ** 0.5, 2),
        "min": round(min(values), 2), "max": round(max(values), 2),
        "spread_pct": round((max(values) - min(values)) / mean * 100, 1) if mean else 0.0,
        "runs": [round(v, 2) for v in values],
    }


def measure(url: str, total_mb: float, msg_mb: float, repeat: int,
            start_at: float = 0.0) -> dict:
    import pika

    msg_bytes = max(1, int(msg_mb * 1e6))
    count = max(1, int(round(total_mb / msg_mb)))
    payload = os.urandom(msg_bytes)          # incompressible: no free wins
    queue = "splitinf_bw_%s" % uuid.uuid4().hex[:10]

    params = pika.URLParameters(url)
    params.heartbeat = 30
    params.blocked_connection_timeout = 30
    params.socket_timeout = 15

    connect_start = time.perf_counter()
    conn = pika.BlockingConnection(params)
    connect_ms = (time.perf_counter() - connect_start) * 1000.0

    try:
        channel = conn.channel()
        channel.queue_declare(queue=queue, exclusive=True, auto_delete=True, durable=False)
        channel.confirm_delivery()           # each publish waits for the ack

        props = pika.BasicProperties(delivery_mode=1)

        # Wait for the agreed start, so a contention pass really is contended.
        #
        # Each device is launched over its own SSH channel and connects at its
        # own pace; without this, whoever got there first publishes part of its
        # data with the broker to itself and reports a throughput the others
        # never see. Measured on three co-located VMs, the first device read
        # 8.84 MB/s against 4.8 for the other two, purely from the head start.
        #
        # The wait is capped: a device whose clock is badly out should be a
        # visible skew in the result, not a two-minute hang.
        skew_ms = 0.0
        if start_at > 0:
            delay = start_at - time.time()
            if delay > 0:
                time.sleep(min(delay, MAX_START_WAIT_S))
            skew_ms = (time.time() - start_at) * 1000.0

        rounds = []
        for _ in range(repeat):
            start = time.perf_counter()
            for _ in range(count):
                channel.basic_publish(
                    exchange="", routing_key=queue, body=payload, properties=props
                )
            elapsed = time.perf_counter() - start
            if elapsed > 0:
                rounds.append(msg_bytes * count / 1e6 / elapsed)
    finally:
        try:
            conn.close()                     # takes the queue with it
        except Exception:                    # noqa: BLE001
            pass

    if not rounds:
        return {"ok": False, "error": "every publish round measured zero time"}

    stats = _stats(rounds)
    return {
        "ok": True,
        # The mean: one round is a sample of whatever the broker and the
        # hypervisor were doing that instant, and on co-located VMs that moves
        # the figure by more than the machines actually differ.
        "mb_s": stats["mean"],
        "stats": stats,
        "mb": round(msg_bytes * count / 1e6, 2),
        "rounds": len(rounds),
        # How far off the agreed start this device actually was. Large values
        # mean the clocks disagree and the contention figure is worth less.
        "start_skew_ms": round(skew_ms, 1),
        "messages": count,
        "message_mb": round(msg_bytes / 1e6, 2),
        "connect_ms": round(connect_ms, 2),
        "broker": "%s:%s" % (params.host, params.port),
        "host": platform.node(),
        "pika": pika.__version__,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mb", type=float, default=32.0,
                    help="total megabytes to publish (default: 32)")
    ap.add_argument("--msg-mb", type=float, default=2.0,
                    help="megabytes per message (default: 2, near the pipeline's own)")
    ap.add_argument("--repeat", type=int, default=3,
                    help="publish rounds to average (default: 3)")
    ap.add_argument("--start-at", type=float, default=0.0,
                    help="unix time to begin publishing, so several devices can "
                         "be made to contend properly (default: start at once)")
    args = ap.parse_args()

    url = os.environ.get("SPLITINF_AMQP_URL", "").strip()
    if not url:
        print(json.dumps({"ok": False, "error": "SPLITINF_AMQP_URL is not set"}))
        return 0

    try:
        result = measure(url, max(1.0, args.mb), max(0.05, args.msg_mb),
                         max(1, args.repeat), args.start_at)
    except Exception as exc:                                  # noqa: BLE001
        # Data, not a traceback: the caller reads one JSON line, and a stack
        # trace on stderr would tell it only that *something* went wrong.
        result = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}

    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
