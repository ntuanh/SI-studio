"""Port of the UI's simulation math.

Source of truth: the DC logic inside `split-inference-pipeline.html`
(`baseLayers`, `models`, `msgMB`, `safeCuts`, `chooseCut`, `e2eAt`, `simCluster`).
Every formula here is line-for-line equivalent so live results stay comparable
with the in-browser simulator (guide §11).

JS -> Python numeric parity notes:
  * `+(x).toFixed(3)` and `Math.round(x)` round half AWAY FROM ZERO; Python's
    built-in `round` is banker's rounding. `_fixed` / `_round` below restore the
    JS behaviour so scaled model layers match byte-for-byte.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Iterable

from ..config import settings


# ---------------------------------------------------------------- JS numerics
def _fixed(x: float, digits: int) -> float:
    """Equivalent of JS `+(x).toFixed(digits)`."""
    q = Decimal(1).scaleb(-digits)
    return float(Decimal(repr(float(x))).quantize(q, rounding=ROUND_HALF_UP))


def _round(x: float) -> int:
    """Equivalent of JS `Math.round(x)` for non-negative values."""
    return int(Decimal(repr(float(x))).quantize(Decimal(1), rounding=ROUND_HALF_UP))


# ------------------------------------------------------------------- model zoo
@dataclass(frozen=True)
class Layer:
    name: str
    flops: float  # GFLOPs for this layer
    bytes: int  # size of this layer's OUTPUT activation, fp32, in bytes


@dataclass(frozen=True)
class Model:
    name: str
    label: str
    layers: tuple[Layer, ...]


#: `baseLayers()` in the UI -- YOLOv11n.
BASE_LAYERS: tuple[Layer, ...] = (
    Layer("Conv-0", 0.35, 3_211_264),
    Layer("Conv-1", 0.52, 1_605_632),
    Layer("C3k2-2", 0.78, 1_605_632),
    Layer("Conv-3", 0.61, 802_816),
    Layer("C3k2-4", 0.94, 802_816),
    Layer("Conv-5", 0.55, 401_408),
    Layer("C3k2-6", 0.72, 401_408),
    Layer("Conv-7", 0.48, 200_704),
    Layer("C3k2-8", 0.66, 200_704),
    Layer("SPPF-9", 0.39, 200_704),
    Layer("C2PSA-10", 0.42, 200_704),
    Layer("Head-11", 0.58, 100_352),
)


def _scale(base: Iterable[Layer], f: float) -> tuple[Layer, ...]:
    """UI: `scale(f)` -- flops scale linearly, activation bytes cap at 1.6x."""
    return tuple(
        Layer(l.name, _fixed(l.flops * f, 3), _round(l.bytes * min(1.6, f))) for l in base
    )


BUILTIN_MODELS: dict[str, Model] = {
    "yolov11n": Model("yolov11n", "YOLOv11n", BASE_LAYERS),
    "yolov11s": Model("yolov11s", "YOLOv11s", _scale(BASE_LAYERS, 2.6)),
    "yolo26n": Model("yolo26n", "YOLO26n", _scale(BASE_LAYERS, 1.15)),
}

DEFAULT_MODEL = "yolov11n"


def model_from_layers(name: str, label: str, layers: list[dict[str, Any]]) -> Model:
    """Build a Model from the UI's uploaded-model JSON (`[{name,flops,bytes}]`)."""
    parsed = tuple(
        Layer(
            str(l.get("name") or f"layer-{i}"),
            float(l.get("flops") or 0.0),
            int(l.get("bytes") or 0),
        )
        for i, l in enumerate(layers)
    )
    if not parsed:
        raise ValueError("model must have at least one layer")
    return Model(name, label or name, parsed)


def get_model(name: str | None, extra: dict[str, Model] | None = None) -> Model:
    """UI: `getModel(name)` -- unknown names fall back to the default model."""
    if extra and name in extra:
        return extra[name]
    if name in BUILTIN_MODELS:
        return BUILTIN_MODELS[name]
    return BUILTIN_MODELS[DEFAULT_MODEL]


# ------------------------------------------------------------------- helpers
def max_mb() -> float:
    """UI: `maxMb()` -- the `maxMessageMb` prop (default 15)."""
    return float(settings.max_message_mb)


def factor(num_bit: int) -> float:
    """UI: `factor(nb)` -- quantization ratio times a 0.65 compression factor."""
    return (num_bit / 32.0) * 0.65


def msg_mb(nbytes: float, num_bit: int) -> float:
    """UI: `msgMB(bytes, nb)`."""
    return nbytes * factor(num_bit) / 1e6


def prefix(layers: tuple[Layer, ...]) -> list[float]:
    """UI: `prefix(layers)` -- cumulative GFLOPs, length len+1."""
    cum = [0.0]
    for l in layers:
        cum.append(cum[-1] + l.flops)
    return cum


def safe_cuts(layers: tuple[Layer, ...], num_bit: int) -> list[int]:
    """UI: `safeCuts(layers, nb)` -- cuts whose message fits the size guard."""
    return [
        c
        for c in range(1, len(layers))
        if msg_mb(layers[c - 1].bytes, num_bit) <= max_mb()
    ]


# ------------------------------------------------------------------- metrics
@dataclass
class CutMetrics:
    edge_ms: float
    cloud_ms: float
    transfer_ms: float
    msg_mb: float
    e2e_ms: float
    bottleneck_ms: float
    fps: float


def e2e_at(
    layers: tuple[Layer, ...],
    cum: list[float],
    total: float,
    cut: int,
    edge_gflops: float,
    cloud_gflops: float,
    num_bit: int,
    bandwidth_mb_s: float,
    latency_ms: float,
) -> CutMetrics:
    """UI: `e2eAt(...)`.

    edge_ms      = cum[cut] / edgeG * 1000
    cloud_ms     = (total - cum[cut]) / cloudG * 1000
    transfer_ms  = msg_mb / bw * 1000 + lat
    bottleneck   = max(edge, transfer, cloud)   -> fps = 1000 / bottleneck
    e2e          = edge + transfer + cloud      (pipeline latency, not period)
    """
    edge_ms = cum[cut] / edge_gflops * 1000.0
    cloud_ms = (total - cum[cut]) / cloud_gflops * 1000.0
    msg = msg_mb(layers[cut - 1].bytes, num_bit)
    transfer_ms = (msg / bandwidth_mb_s) * 1000.0 + latency_ms
    bottleneck = max(edge_ms, transfer_ms, cloud_ms)
    return CutMetrics(
        edge_ms=edge_ms,
        cloud_ms=cloud_ms,
        transfer_ms=transfer_ms,
        msg_mb=msg,
        e2e_ms=edge_ms + transfer_ms + cloud_ms,
        bottleneck_ms=bottleneck,
        fps=(1000.0 / bottleneck) if bottleneck > 0 else 0.0,
    )


def choose_cut(
    layers: tuple[Layer, ...],
    edge_gflops: float,
    cloud_gflops: float,
    mode: str,
    num_bit: int,
    bandwidth_mb_s: float,
    latency_ms: float,
) -> int:
    """UI: `chooseCut(...)`.

    mode == 'latency' -> minimize end-to-end latency.
    otherwise ('power') -> split FLOPs proportionally to compute capacity.
    """
    cum = prefix(layers)
    total = cum[len(layers)]
    safe = safe_cuts(layers, num_bit)
    cand = safe if safe else [len(layers) - 1]

    if mode == "latency":
        best, best_e = cand[0], float("inf")
        for c in cand:
            e = e2e_at(
                layers, cum, total, c, edge_gflops, cloud_gflops, num_bit,
                bandwidth_mb_s, latency_ms,
            ).e2e_ms
            if e < best_e:
                best_e, best = e, c
        return best

    share = edge_gflops / (edge_gflops + cloud_gflops)
    best, best_d = cand[0], float("inf")
    for c in cand:
        d = abs(cum[c] / total - share)
        if d < best_d:
            best_d, best = d, c
    return best


# ------------------------------------------------------- cluster-level result
@dataclass
class DeviceSpec:
    """Minimal device view the math needs (mirrors the UI device object)."""

    id: str
    name: str
    side: str  # edge | cloud
    gflops: float
    bandwidth_mb_s: float
    latency_ms: float


@dataclass
class ClusterInput:
    id: int
    queue_name: str
    model_name: str
    num_bit: int
    batch_size: int
    edges: list[DeviceSpec]
    clouds: list[DeviceSpec]
    split_override: int | None = None


@dataclass
class ClusterMetrics:
    cluster: int
    cut: int
    layer_count: int
    edge_ms: float
    transfer_ms: float
    cloud_ms: float
    e2e_ms: float
    msg_mb: float
    fps: float
    bottleneck_ms: float
    edge_util: float
    transfer_util: float
    cloud_util: float
    edge_gflops: float
    cloud_gflops: float
    bandwidth_mb_s: float
    latency_ms: float
    total_gflops: float
    guard_hit: bool
    devices: list[dict[str, Any]]

    def to_payload(self, *, queue_depth: int = 0, source: str = "sim") -> dict[str, Any]:
        """The §6 wire shape the UI renders."""
        return {
            "cluster": self.cluster,
            "cut": self.cut,
            "layer_count": self.layer_count,
            "edge_ms": round(self.edge_ms, 3),
            "transfer_ms": round(self.transfer_ms, 3),
            "cloud_ms": round(self.cloud_ms, 3),
            "e2e_ms": round(self.e2e_ms, 3),
            "msg_mb": round(self.msg_mb, 4),
            "fps": round(self.fps, 3),
            "edge_util": round(self.edge_util, 4),
            "transfer_util": round(self.transfer_util, 4),
            "cloud_util": round(self.cloud_util, 4),
            "queue_depth": queue_depth,
            "devices": self.devices,
            # extras (ignored by the UI, handy for the console / CSV parity)
            "edge_gflops": self.edge_gflops,
            "cloud_gflops": self.cloud_gflops,
            "bandwidth_mb_s": self.bandwidth_mb_s,
            "latency_ms": round(self.latency_ms, 3),
            "guard_hit": self.guard_hit,
            "source": source,
        }


def sim_cluster(
    cl: ClusterInput,
    *,
    auto_balance: str = "power",
    manual_enabled: bool = False,
    manual_split: int = 5,
    extra_models: dict[str, Model] | None = None,
) -> ClusterMetrics | None:
    """UI: `simCluster(cl)`. Returns None for an idle cluster (needs both sides)."""
    if not cl.edges or not cl.clouds:
        return None

    layers = get_model(cl.model_name, extra_models).layers
    cum = prefix(layers)
    total = cum[len(layers)]

    edge_g = sum(d.gflops or 0.0 for d in cl.edges)
    cloud_g = sum(d.gflops or 0.0 for d in cl.clouds)
    bw = min((d.bandwidth_mb_s or 1.0) for d in cl.edges)
    lat = (
        sum(d.latency_ms or 0.0 for d in cl.edges) / len(cl.edges)
        + sum(d.latency_ms or 0.0 for d in cl.clouds) / len(cl.clouds)
    )

    # --- cut selection: per-cluster override > global manual > auto ---
    guard_hit = False
    if cl.split_override is not None:
        cut = min(len(layers) - 1, max(1, cl.split_override))
    elif manual_enabled:
        cut = min(len(layers) - 1, max(1, manual_split))
    else:
        cut = choose_cut(layers, edge_g or 1.0, cloud_g or 1.0, auto_balance, cl.num_bit, bw, lat)

    # --- message-size guard: snap to the first safe cut if we overshoot ---
    safe = safe_cuts(layers, cl.num_bit)
    if safe and cut not in safe and msg_mb(layers[cut - 1].bytes, cl.num_bit) > max_mb():
        guard_hit = True
        cut = safe[0]

    m = e2e_at(layers, cum, total, cut, edge_g or 1.0, cloud_g or 1.0, cl.num_bit, bw, lat)
    bn = m.bottleneck_ms or 1.0

    # Per-device util: each side's stage time weighted by that device's share of
    # its side's compute, expressed against the pipeline bottleneck.
    devices: list[dict[str, Any]] = []
    for d in cl.edges:
        share = (d.gflops / edge_g) if edge_g else 0.0
        devices.append({"id": d.id, "util": round(min(1.0, m.edge_ms * share / bn), 4), "role": "head"})
    for d in cl.clouds:
        share = (d.gflops / cloud_g) if cloud_g else 0.0
        devices.append({"id": d.id, "util": round(min(1.0, m.cloud_ms * share / bn), 4), "role": "tail"})

    return ClusterMetrics(
        cluster=cl.id,
        cut=cut,
        layer_count=len(layers),
        edge_ms=m.edge_ms,
        transfer_ms=m.transfer_ms,
        cloud_ms=m.cloud_ms,
        e2e_ms=m.e2e_ms,
        msg_mb=m.msg_mb,
        fps=m.fps,
        bottleneck_ms=m.bottleneck_ms,
        edge_util=m.edge_ms / bn,
        transfer_util=m.transfer_ms / bn,
        cloud_util=m.cloud_ms / bn,
        edge_gflops=edge_g,
        cloud_gflops=cloud_g,
        bandwidth_mb_s=bw,
        latency_ms=lat,
        total_gflops=total,
        guard_hit=guard_hit,
        devices=devices,
    )
