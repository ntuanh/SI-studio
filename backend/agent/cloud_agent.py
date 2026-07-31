#!/usr/bin/env python3
"""Cloud agent -- runs ON a cloud device (guide §9).

Consume the intermediate queue -> dequantize -> run `layers[cut:]` (tail.pt)
-> NMS -> publish detections and `{cloud_ms, transfer_ms, e2e_ms}` to
`metrics_queue`.

    python3 cloud_agent.py --broker-url amqp://guest:guest@10.0.0.5:5672/ \
        --queue intermediate_queue_1 --model /opt/split-inference/models/tail.pt \
        --cluster 1 --device-id d4 --run-id abc123

`transfer_ms` is `t_consume - t_publish` across two machines, so it is only
meaningful with NTP-synced clocks. When the delta comes out negative (clock
skew), it is clamped to 0 and `clock_skew` is flagged on the report.

Dependencies on the device: torch, numpy, pika (see bootstrap.sh). The tail
shard must be a TorchScript module -- see tools/split_model.py.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import codec  # noqa: E402

log = logging.getLogger("cloud_agent")

_STOP = False


def _handle_signal(signum: int, _frame: Any) -> None:
    global _STOP
    _STOP = True
    log.info("signal %s received; will stop consuming", signum)


# ----------------------------------------------------------------------- NMS
def nms(boxes: np.ndarray, scores: np.ndarray, iou_threshold: float) -> list[int]:
    """Greedy IoU non-max suppression over xyxy boxes."""
    if boxes.size == 0:
        return []
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = np.clip(x2 - x1, 0, None) * np.clip(y2 - y1, 0, None)
    order = scores.argsort()[::-1]

    keep: list[int] = []
    while order.size > 0:
        i = int(order[0])
        keep.append(i)
        if order.size == 1:
            break
        rest = order[1:]
        xx1 = np.maximum(x1[i], x1[rest])
        yy1 = np.maximum(y1[i], y1[rest])
        xx2 = np.minimum(x2[i], x2[rest])
        yy2 = np.minimum(y2[i], y2[rest])
        inter = np.clip(xx2 - xx1, 0, None) * np.clip(yy2 - yy1, 0, None)
        union = areas[i] + areas[rest] - inter
        iou = np.where(union > 0, inter / union, 0.0)
        order = rest[iou <= iou_threshold]
    return keep


def postprocess(raw: np.ndarray, conf: float, iou: float, max_det: int) -> list[dict[str, Any]]:
    """Decode a YOLO-style head output into detections.

    Accepts (N, 4+C) or the transposed (4+C, N) layout that YOLOv8/11 export,
    with xywh boxes and per-class scores. Anything else is returned empty rather
    than guessed at.
    """
    arr = np.asarray(raw, dtype=np.float32)
    if arr.ndim == 3:
        arr = arr[0]
    if arr.ndim != 2:
        return []
    # Heuristic: predictions outnumber channels in the exported layout.
    if arr.shape[0] < arr.shape[1]:
        arr = arr.T
    if arr.shape[1] < 5:
        return []

    xywh, cls_scores = arr[:, :4], arr[:, 4:]
    class_ids = cls_scores.argmax(axis=1)
    confidences = cls_scores.max(axis=1)

    mask = confidences >= conf
    if not mask.any():
        return []
    xywh, class_ids, confidences = xywh[mask], class_ids[mask], confidences[mask]

    cx, cy, w, h = xywh[:, 0], xywh[:, 1], xywh[:, 2], xywh[:, 3]
    boxes = np.stack([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2], axis=1)

    keep = nms(boxes, confidences, iou)[:max_det]
    return [
        {
            "box": [round(float(v), 2) for v in boxes[i]],
            "score": round(float(confidences[i]), 4),
            "class": int(class_ids[i]),
        }
        for i in keep
    ]


# -------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description="Split-inference cloud agent")
    ap.add_argument("--broker-url", required=True)
    ap.add_argument("--queue", required=True, help="intermediate_queue_<cluster>")
    ap.add_argument("--model", required=True, help="tail.pt (TorchScript)")
    ap.add_argument("--metrics-queue", default="metrics_queue")
    ap.add_argument("--fps-queue", default="fps_queue")
    ap.add_argument("--detections-queue", default="", help="publish detections here if set")
    ap.add_argument("--cluster", type=int, default=1)
    ap.add_argument("--device-id", default=os.uname().nodename if hasattr(os, "uname") else "cloud")
    ap.add_argument("--run-id", default="")
    ap.add_argument("--conf", type=float, default=0.25)
    ap.add_argument("--iou", type=float, default=0.45)
    ap.add_argument("--max-det", type=int, default=300)
    ap.add_argument("--prefetch", type=int, default=8)
    ap.add_argument("--torch-device", default="auto", choices=["auto", "cuda", "cpu"])
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s cloud[%(name)s]: %(message)s",
    )
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    import pika
    import torch

    device = (
        ("cuda" if torch.cuda.is_available() else "cpu")
        if args.torch_device == "auto"
        else args.torch_device
    )
    log.info("loading tail shard %s on %s", args.model, device)
    tail = torch.jit.load(args.model, map_location=device)
    tail.eval()

    params = pika.URLParameters(args.broker_url)
    params.heartbeat = 60
    params.blocked_connection_timeout = 30
    conn = pika.BlockingConnection(params)
    channel = conn.channel()
    channel.queue_declare(queue=args.queue, durable=True)
    channel.queue_declare(queue=args.metrics_queue, durable=True)
    channel.queue_declare(queue=args.fps_queue, durable=True)
    if args.detections_queue:
        channel.queue_declare(queue=args.detections_queue, durable=True)
    channel.basic_qos(prefetch_count=max(1, args.prefetch))

    consumed = 0
    window_start = time.perf_counter()
    window_frames = 0
    persistent = pika.BasicProperties(delivery_mode=2, content_type="application/json")

    def on_message(ch: Any, method: Any, _props: Any, body: bytes) -> None:
        nonlocal consumed, window_frames, window_start
        t_consume = time.time()

        try:
            fmap, header = codec.decode(body)
        except (ValueError, KeyError) as exc:
            log.warning("dropping malformed frame: %s", exc)
            ch.basic_ack(delivery_tag=method.delivery_tag)
            return

        # --- transfer time across the broker (needs synced clocks) ---
        raw_transfer = (t_consume - float(header.get("t_publish", t_consume))) * 1000.0
        clock_skew = raw_transfer < 0
        transfer_ms = max(0.0, raw_transfer)

        # --- tail forward: layers[cut:] ---
        t0 = time.perf_counter()
        try:
            with torch.no_grad():
                out = tail(torch.from_numpy(fmap).to(device))
            if device == "cuda":
                torch.cuda.synchronize()
            raw = (out[0] if isinstance(out, (list, tuple)) else out).detach().cpu().numpy()
        except Exception as exc:  # a bad frame must not kill the consumer
            log.exception("tail forward failed: %s", exc)
            ch.basic_ack(delivery_tag=method.delivery_tag)
            return
        infer_ms = (time.perf_counter() - t0) * 1000.0

        # --- NMS ---
        t1 = time.perf_counter()
        detections = postprocess(raw, args.conf, args.iou, args.max_det)
        nms_ms = (time.perf_counter() - t1) * 1000.0
        cloud_ms = infer_ms + nms_ms

        edge_ms = float(header.get("edge_ms") or 0.0)
        wire_bytes = int(header.get("wire_bytes") or len(body))

        report = {
            "cluster": int(header.get("cluster", args.cluster)),
            "run_id": header.get("run_id") or args.run_id,
            "frame_id": header.get("frame_id"),
            "cut": header.get("cut"),
            "device_id": args.device_id,
            "edge_device_id": header.get("edge_device_id"),
            "edge_ms": round(edge_ms, 3),
            "transfer_ms": round(transfer_ms, 3),
            "cloud_ms": round(cloud_ms, 3),
            "infer_ms": round(infer_ms, 3),
            "nms_ms": round(nms_ms, 3),
            "e2e_ms": round(edge_ms + transfer_ms + cloud_ms, 3),
            "msg_mb": round(wire_bytes / 1e6, 5),
            "raw_bytes": header.get("raw_bytes"),
            "num_bit": header.get("num_bit"),
            "detections": len(detections),
            "clock_skew": clock_skew,
            "ts": t_consume,
        }
        ch.basic_publish("", args.metrics_queue, json.dumps(report).encode(), persistent)

        if args.detections_queue:
            ch.basic_publish(
                "",
                args.detections_queue,
                json.dumps(
                    {
                        "cluster": report["cluster"],
                        "frame_id": report["frame_id"],
                        "detections": detections,
                    }
                ).encode(),
                persistent,
            )

        ch.basic_ack(delivery_tag=method.delivery_tag)
        consumed += 1
        window_frames += 1

        # --- per-second throughput report on fps_queue ---
        elapsed = time.perf_counter() - window_start
        if elapsed >= 1.0:
            ch.basic_publish(
                "",
                args.fps_queue,
                json.dumps(
                    {
                        "cluster": report["cluster"],
                        "device_id": args.device_id,
                        "run_id": report["run_id"],
                        "fps": round(window_frames / elapsed, 3),
                        "ts": time.time(),
                    }
                ).encode(),
                persistent,
            )
            window_start, window_frames = time.perf_counter(), 0

        if consumed % 50 == 0:
            log.info(
                "consumed %d frames (cloud_ms=%.2f transfer_ms=%.2f dets=%d)",
                consumed, cloud_ms, transfer_ms, len(detections),
            )
        if _STOP:
            ch.stop_consuming()

    channel.basic_consume(queue=args.queue, on_message_callback=on_message)
    log.info("consuming %s -> %s", args.queue, args.metrics_queue)

    try:
        channel.start_consuming()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            channel.stop_consuming()
            conn.close()
        except Exception:
            pass
        log.info("exiting after %d frames", consumed)

    print(json.dumps({"ok": True, "consumed": consumed}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
