"""Parity between `app/inference/simulation.py` and the UI's JS math.

`golden_ui_math.tsv` was produced by running the formulas from
`split-inference-pipeline.html` verbatim in an independent JavaScript engine
(Windows JScript via `cscript`), so this is a real cross-implementation check
rather than a restatement of the Python. If the UI's formulas change, re-run
that harness and refresh the golden file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.inference import simulation as sim

GOLDEN = Path(__file__).parent / "golden_ui_math.tsv"

EDGE = [("Jetson-A", 472, 12, 6, 1), ("Jetson-B", 472, 12, 6, 1), ("Jetson-C", 384, 10, 8, 2)]
CLOUD = [("GPU-1", 9800, 125, 2, 1), ("GPU-2", 9800, 125, 2, 2)]

#: name -> (auto_balance, manual_enabled, manual_split, model, num_bit, override)
SCENARIOS = {
    "power-8bit-n": ("power", False, 5, "yolov11n", 8, None),
    "latency-8bit-n": ("latency", False, 5, "yolov11n", 8, None),
    "power-4bit-n": ("power", False, 5, "yolov11n", 4, None),
    "power-16bit-s": ("power", False, 5, "yolov11s", 16, None),
    "latency-32bit-s": ("latency", False, 5, "yolov11s", 32, None),
    "manual5-n": ("power", True, 5, "yolov11n", 8, None),
    "manual99-n": ("power", True, 99, "yolov11n", 8, None),
    "override3-26n": ("power", False, 5, "yolo26n", 8, 3),
    "override0-26n": ("power", False, 5, "yolo26n", 8, 0),
}


def _specs(rows, side, k):
    return [
        sim.DeviceSpec(id=n, name=n, side=side, gflops=g, bandwidth_mb_s=b, latency_ms=l)
        for (n, g, b, l, c) in rows
        if c == k
    ]


def _golden_rows(kind: str) -> list[list[str]]:
    rows = []
    for line in GOLDEN.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        is_layer = parts[0] == "LAYER"
        if (kind == "layer") == is_layer:
            rows.append(parts)
    return rows


def _ids(rows):
    return [f"{r[0]}-{r[1]}" for r in rows]


CLUSTER_ROWS = _golden_rows("cluster")
LAYER_ROWS = _golden_rows("layer")


def test_golden_file_is_populated():
    assert len(CLUSTER_ROWS) == 18  # 9 scenarios x 2 clusters
    assert len(LAYER_ROWS) == 36  # 3 models x 12 layers


@pytest.mark.parametrize("row", CLUSTER_ROWS, ids=_ids(CLUSTER_ROWS))
def test_cluster_metrics_match_the_ui(row):
    scenario, cluster_tag = row[0], row[1]
    k = int(cluster_tag.lstrip("c"))
    mode, manual, msplit, model, num_bit, override = SCENARIOS[scenario]

    cl = sim.ClusterInput(
        id=k, queue_name=f"intermediate_queue_{k}", model_name=model,
        num_bit=num_bit, batch_size=32,
        edges=_specs(EDGE, "edge", k), clouds=_specs(CLOUD, "cloud", k),
        split_override=override,
    )
    m = sim.sim_cluster(cl, auto_balance=mode, manual_enabled=manual, manual_split=msplit)
    assert m is not None

    (cut, edge_ms, transfer_ms, cloud_ms, e2e_ms, msg_mb, fps,
     edge_util, transfer_util, cloud_util, bw, lat, guard) = row[2:]

    assert m.cut == int(cut)
    assert m.edge_ms == pytest.approx(float(edge_ms), abs=1e-6)
    assert m.transfer_ms == pytest.approx(float(transfer_ms), abs=1e-6)
    assert m.cloud_ms == pytest.approx(float(cloud_ms), abs=1e-6)
    assert m.e2e_ms == pytest.approx(float(e2e_ms), abs=1e-6)
    assert m.msg_mb == pytest.approx(float(msg_mb), abs=1e-8)
    assert m.fps == pytest.approx(float(fps), abs=1e-6)
    assert m.edge_util == pytest.approx(float(edge_util), abs=1e-6)
    assert m.transfer_util == pytest.approx(float(transfer_util), abs=1e-6)
    assert m.cloud_util == pytest.approx(float(cloud_util), abs=1e-6)
    assert m.bandwidth_mb_s == pytest.approx(float(bw), abs=1e-9)
    assert m.latency_ms == pytest.approx(float(lat), abs=1e-6)
    assert m.guard_hit == bool(int(guard))


@pytest.mark.parametrize("row", LAYER_ROWS, ids=lambda r: f"{r[1]}-{r[2]}")
def test_model_layer_tables_match_the_ui(row):
    """Catches drift in `scale()`'s toFixed(3) / Math.round behaviour."""
    _, model, index, flops, nbytes = row
    layer = sim.BUILTIN_MODELS[model].layers[int(index)]
    assert layer.flops == float(flops)
    assert layer.bytes == int(nbytes)


# ---------------------------------------------------------------- unit checks
def test_js_rounding_helpers():
    """JS rounds halves away from zero; Python's round() is banker's rounding."""
    assert sim._round(0.5) == 1  # Python's round(0.5) would give 0
    assert sim._round(1.5) == 2
    assert sim._round(2.5) == 3  # round(2.5) would give 2
    assert sim._fixed(0.6325, 3) == 0.633
    assert sim._fixed(1.0005, 3) == 1.001


def test_idle_cluster_returns_none():
    edges = _specs(EDGE, "edge", 1)
    base = dict(id=1, queue_name="q", model_name="yolov11n", num_bit=8, batch_size=32)
    assert sim.sim_cluster(sim.ClusterInput(**base, edges=edges, clouds=[])) is None
    assert sim.sim_cluster(sim.ClusterInput(**base, edges=[], clouds=_specs(CLOUD, "cloud", 1))) is None


def test_message_size_guard_snaps_to_a_safe_cut():
    """A model whose early activations blow past MAX_MESSAGE_MB must be cut later."""
    huge = sim.Model(
        "huge", "Huge",
        tuple(
            [sim.Layer("big-0", 1.0, 800_000_000)]  # ~16 MB at 8 bits -> unsafe
            + [sim.Layer(f"small-{i}", 1.0, 100_000) for i in range(1, 5)]
        ),
    )
    layers = huge.layers
    safe = sim.safe_cuts(layers, 8)
    assert 1 not in safe and safe  # cut 1 would ship the oversized activation

    cl = sim.ClusterInput(
        id=1, queue_name="q", model_name="huge", num_bit=8, batch_size=32,
        edges=_specs(EDGE, "edge", 1), clouds=_specs(CLOUD, "cloud", 1),
        split_override=1,  # deliberately ask for the unsafe cut
    )
    m = sim.sim_cluster(cl, extra_models={"huge": huge})
    assert m is not None
    assert m.guard_hit is True
    assert m.cut == safe[0]


def test_unknown_model_falls_back_to_the_default():
    assert sim.get_model("does-not-exist").name == sim.DEFAULT_MODEL
    assert sim.get_model(None).name == sim.DEFAULT_MODEL


def test_per_device_util_is_bounded_and_split_by_role():
    cl = sim.ClusterInput(
        id=1, queue_name="q", model_name="yolov11n", num_bit=8, batch_size=32,
        edges=_specs(EDGE, "edge", 1), clouds=_specs(CLOUD, "cloud", 1),
    )
    m = sim.sim_cluster(cl)
    assert m is not None
    assert {d["id"] for d in m.devices} == {"Jetson-A", "Jetson-B", "GPU-1"}
    assert {d["role"] for d in m.devices} == {"head", "tail"}
    assert all(0.0 <= d["util"] <= 1.0 for d in m.devices)


def test_payload_carries_every_key_the_ui_reads():
    cl = sim.ClusterInput(
        id=1, queue_name="q", model_name="yolov11n", num_bit=8, batch_size=32,
        edges=_specs(EDGE, "edge", 1), clouds=_specs(CLOUD, "cloud", 1),
    )
    payload = sim.sim_cluster(cl).to_payload(queue_depth=3)
    for key in (
        "cluster", "cut", "edge_ms", "transfer_ms", "cloud_ms", "e2e_ms",
        "msg_mb", "fps", "edge_util", "transfer_util", "cloud_util",
        "queue_depth", "devices",
    ):
        assert key in payload, key
    assert payload["queue_depth"] == 3
