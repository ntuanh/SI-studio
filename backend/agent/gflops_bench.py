#!/usr/bin/env python3
"""Effective FP32 throughput of this machine, in GFLOPS.

Pushed to every device by `app/services/measure.py` and run there; prints one
JSON line on stdout and nothing else, so the caller can parse it without
guessing where the output starts.

    python3 gflops_bench.py --batch 16 --budget 0.8

What it measures, and why that
------------------------------
A **3x3 convolution**, not a square matmul. The pipeline runs a CNN, and a CNN
reaches a very different fraction of peak than a big GEMM does -- on most GPUs
the matmul figure is several times the convolution figure. The control plane
turns this number straight into milliseconds (`cum[cut] / gflops`), so the
optimistic one would become an optimistic schedule and a cut layer placed in
the wrong place. The matmul is measured too, and reported, but only as a
cross-check.

dtype and TF32 are left at their defaults on purpose: that is what the agent's
own forward pass runs under, so it is what should be timed.

Iteration counts are not fixed. One warm-up call is timed, and the count is
chosen from it so the measured loop runs for `--budget` seconds -- a Jetson and
an H100 differ by three orders of magnitude, and any constant would either take
a minute on one or be pure clock noise on the other.

The whole thing is then repeated `--repeat` times and **averaged**, which the
iteration count alone cannot substitute for: a long loop still samples a single
moment, and on a virtual machine that moment includes whatever the neighbours
on the same host were doing. The spread across runs is reported alongside the
mean, so a figure taken during a noisy minute is visible as one.

Exit status is 0 even when nothing could be measured: the failure is reported in
the JSON (`{"ok": false, "error": "..."}`), because a non-zero exit with the
reason on stderr is a reason the caller throws away.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time


def _stats(values: list[float]) -> dict:
    """Mean, spread and extremes of repeated runs.

    The mean is what gets used. A single run on a virtual machine is a sample
    of whatever the hypervisor was doing that second, and neighbours on the
    same host move it by more than the hardware difference this is supposed to
    be measuring -- `spread_pct` is how much to distrust the answer.
    """
    n = len(values)
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / n if n > 1 else 0.0
    return {
        "mean": round(mean, 1),
        "std": round(variance ** 0.5, 1),
        "min": round(min(values), 1),
        "max": round(max(values), 1),
        "spread_pct": round((max(values) - min(values)) / mean * 100, 1) if mean else 0.0,
        "runs": [round(v, 1) for v in values],
    }


def _timed(fn, sync, budget: float) -> tuple[float, int]:
    """Run `fn` enough times to fill `budget` seconds. Returns (elapsed, iters)."""
    fn()
    sync()                                    # warm up: allocation, algo choice

    start = time.perf_counter()
    fn()
    sync()
    once = max(time.perf_counter() - start, 1e-6)

    iters = max(3, min(500, int(budget / once)))
    start = time.perf_counter()
    for _ in range(iters):
        fn()
    sync()
    return time.perf_counter() - start, iters


def benchmark(batch: int, budget: float, repeat: int) -> dict:
    import torch
    import torch.nn.functional as F

    cuda = torch.cuda.is_available()
    device = "cuda" if cuda else "cpu"
    sync = torch.cuda.synchronize if cuda else (lambda: None)

    # cudnn picks its algorithm on the first call for a given shape. Without
    # this the warm-up run times the search rather than the convolution.
    torch.backends.cudnn.benchmark = True

    out = {
        "ok": True,
        "device": device,
        "torch": torch.__version__,
        "host": platform.node(),
        "python": platform.python_version(),
    }
    if cuda:
        out["gpu"] = torch.cuda.get_device_name(0)
        out["gpu_count"] = torch.cuda.device_count()

    with torch.no_grad():
        # --- 3x3 convolution: where a CNN stage actually spends its time ---
        channels, size = (64, 64) if cuda else (32, 64)
        n = batch if cuda else min(batch, 8)   # a CPU at batch 32 is a coffee break
        x = torch.randn(n, channels, size, size, device=device)
        w = torch.randn(channels, channels, 3, 3, device=device)

        # MACs = Cout*Hout*Wout*Cin*K*K per image; FLOPs = 2*MACs.
        per_image = 2.0 * 9 * channels * channels * size * size * n
        conv_runs = []
        for _ in range(repeat):
            elapsed, iters = _timed(lambda: F.conv2d(x, w, padding=1), sync, budget)
            conv_runs.append(per_image * iters / elapsed / 1e9)
        conv = _stats(conv_runs)
        out["conv_gflops"] = conv["mean"]
        out["conv_stats"] = conv
        out["conv_shape"] = [n, channels, size, size]

        # --- square matmul: the classic figure, for comparison only ---
        m = 4096 if cuda else 1024
        a = torch.randn(m, m, device=device)
        b = torch.randn(m, m, device=device)
        gemm_runs = []
        for _ in range(repeat):
            elapsed, iters = _timed(lambda: a @ b, sync, budget)
            gemm_runs.append(2.0 * m**3 * iters / elapsed / 1e9)
        gemm = _stats(gemm_runs)
        out["gemm_gflops"] = gemm["mean"]
        out["gemm_stats"] = gemm
        out["gemm_n"] = m

    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--batch", type=int, default=16,
                    help="images per forward pass (default: 16)")
    ap.add_argument("--budget", type=float, default=0.8,
                    help="seconds to spend on each benchmark run (default: 0.8)")
    ap.add_argument("--repeat", type=int, default=3,
                    help="independent runs to average (default: 3)")
    args = ap.parse_args()

    try:
        result = benchmark(max(1, args.batch), max(0.05, args.budget),
                           max(1, args.repeat))
    except Exception as exc:                                  # noqa: BLE001
        # Reported as data, never as a traceback: the caller reads one JSON
        # line, and a stack trace on stderr tells it only that *something*
        # went wrong.
        result = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}

    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
