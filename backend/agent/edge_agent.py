#!/usr/bin/env python3
"""Edge agent -- runs ON an edge device (guide §9).

Loop: get a frame -> run `layers[:cut]` (head.pt) -> quantize to `--num-bit`
-> publish to the cluster's intermediate queue -> record `edge_ms`.

    python3 edge_agent.py --broker-url amqp://guest:guest@10.0.0.5:5672/ \
        --queue intermediate_queue_1 --model /opt/split-inference/models/head.pt \
        --cut 6 --num-bit 8 --batch 32 --cluster 1 --device-id d1 --run-id abc123

Frame source, in order of preference: `--source` (a video file, an image
directory, or a camera index), otherwise synthetic noise so the pipeline can be
exercised without a camera attached.

Dependencies on the device: torch, numpy, pika (see bootstrap.sh). The head
shard must be a TorchScript module (`torch.jit.save`) so no model class
definition is needed here -- `tools/split_model.py` produces one.
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
from typing import Any, Iterator

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import codec  # noqa: E402

log = logging.getLogger("edge_agent")

_STOP = False


def _handle_signal(signum: int, _frame: Any) -> None:
    global _STOP
    _STOP = True
    log.info("signal %s received; finishing current frame then exiting", signum)


# ------------------------------------------------------------------ frames
def frame_source(source: str | None, size: int, limit: int | None) -> Iterator[np.ndarray]:
    """Yield HWC uint8 frames."""
    emitted = 0

    def budget_left() -> bool:
        return limit is None or emitted < limit

    if source:
        path = Path(source)
        if path.is_dir():
            images = sorted(
                p for p in path.iterdir()
                if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
            )
            if not images:
                raise SystemExit(f"no images found in {path}")
            try:
                import cv2
            except ImportError as exc:
                raise SystemExit("image-directory source needs opencv-python") from exc
            while budget_left():
                for img_path in images:
                    if _STOP or not budget_left():
                        return
                    img = cv2.imread(str(img_path))
                    if img is None:
                        continue
                    yield cv2.resize(img, (size, size))
                    emitted += 1
            return

        try:
            import cv2
        except ImportError as exc:
            raise SystemExit("video/camera source needs opencv-python") from exc
        handle = int(source) if source.isdigit() else source
        cap = cv2.VideoCapture(handle)
        if not cap.isOpened():
            raise SystemExit(f"could not open source {source!r}")
        try:
            while budget_left() and not _STOP:
                ok, img = cap.read()
                if not ok:  # loop a finished file
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    ok, img = cap.read()
                    if not ok:
                        return
                yield cv2.resize(img, (size, size))
                emitted += 1
        finally:
            cap.release()
        return

    # Synthetic fallback -- keeps the deployment testable without a camera.
    rng = np.random.default_rng(1234)
    while budget_left() and not _STOP:
        yield rng.integers(0, 255, (size, size, 3), dtype=np.uint8)
        emitted += 1


# -------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description="Split-inference edge agent")
    ap.add_argument("--broker-url", required=True)
    ap.add_argument("--queue", required=True, help="intermediate_queue_<cluster>")
    ap.add_argument("--model", required=True, help="head.pt (TorchScript)")
    ap.add_argument("--cut", type=int, required=True)
    ap.add_argument("--num-bit", type=int, default=8)
    ap.add_argument("--batch", type=int, default=1, help="frames per forward pass")
    ap.add_argument("--cluster", type=int, default=1)
    ap.add_argument("--device-id", default=os.uname().nodename if hasattr(os, "uname") else "edge")
    ap.add_argument("--run-id", default="")
    ap.add_argument("--source", default=None, help="video file, image dir, or camera index")
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--max-frames", type=int, default=None)
    ap.add_argument("--fps-limit", type=float, default=0.0, help="0 = unthrottled")
    ap.add_argument("--torch-device", default="auto", choices=["auto", "cuda", "cpu"])
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s edge[%(name)s]: %(message)s",
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
    log.info("loading head shard %s on %s", args.model, device)
    head = torch.jit.load(args.model, map_location=device)
    head.eval()

    params = pika.URLParameters(args.broker_url)
    params.heartbeat = 60
    params.blocked_connection_timeout = 30
    conn = pika.BlockingConnection(params)
    channel = conn.channel()
    channel.queue_declare(queue=args.queue, durable=True)
    log.info("publishing to %s (cut=%d, num_bit=%d, batch=%d)",
             args.queue, args.cut, args.num_bit, args.batch)

    batch = max(1, args.batch)
    min_period = 1.0 / args.fps_limit if args.fps_limit > 0 else 0.0
    frame_id = 0
    published = 0
    buffer: list[np.ndarray] = []
    t_last = 0.0

    try:
        for img in frame_source(args.source, args.imgsz, args.max_frames):
            buffer.append(img)
            if len(buffer) < batch:
                continue

            if min_period and t_last:
                if (wait := min_period - (time.perf_counter() - t_last)) > 0:
                    time.sleep(wait)
            t_last = time.perf_counter()

            # --- preprocess: NHWC uint8 -> NCHW float32 in [0,1] ---
            stack = np.stack(buffer).astype(np.float32) / 255.0
            tensor = torch.from_numpy(stack).permute(0, 3, 1, 2).contiguous().to(device)
            buffer = []

            # --- head forward: layers[:cut] ---
            t0 = time.perf_counter()
            with torch.no_grad():
                out = head(tensor)
            if device == "cuda":
                torch.cuda.synchronize()
            fmap = (out[0] if isinstance(out, (list, tuple)) else out).detach().cpu().numpy()
            edge_ms = (time.perf_counter() - t0) * 1000.0

            # --- quantize + publish ---
            frame_id += 1
            payload = codec.encode(
                fmap,
                args.num_bit,
                {
                    "frame_id": frame_id,
                    "cluster": args.cluster,
                    "cut": args.cut,
                    "batch": batch,
                    "edge_device_id": args.device_id,
                    "run_id": args.run_id,
                    "edge_ms": round(edge_ms, 3),
                    # Wall clock, so the cloud side can derive transfer_ms.
                    # Requires NTP-synced clocks; see README.
                    "t_publish": time.time(),
                },
            )
            channel.basic_publish(
                exchange="",
                routing_key=args.queue,
                body=payload,
                properties=pika.BasicProperties(delivery_mode=2, content_type="application/octet-stream"),
            )
            published += 1

            if published % 50 == 0:
                log.info("published %d frames (last edge_ms=%.2f, msg=%.1f KB)",
                         published, edge_ms, len(payload) / 1024)
            if _STOP:
                break
    except KeyboardInterrupt:
        pass
    finally:
        with_suppress = getattr(conn, "close", lambda: None)
        try:
            with_suppress()
        except Exception:
            pass
        log.info("exiting after %d frames", published)

    print(json.dumps({"ok": True, "published": published}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
