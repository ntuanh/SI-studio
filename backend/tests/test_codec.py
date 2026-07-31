"""The edge->cloud wire format must round-trip at every supported bit width."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))

import codec  # noqa: E402


@pytest.mark.parametrize("num_bit", [2, 4, 6, 8, 12, 16])
def test_round_trip_preserves_shape_and_range(num_bit):
    rng = np.random.default_rng(7)
    original = rng.normal(0, 3, (2, 64, 40, 40)).astype(np.float32)

    frame = codec.encode(original, num_bit, {"frame_id": 1, "cluster": 1})
    restored, header = codec.decode(frame)

    assert restored.shape == original.shape
    assert restored.dtype == np.float32
    assert header["num_bit"] == num_bit
    assert header["frame_id"] == 1
    assert header["raw_bytes"] == original.size * 4

    # Quantization error is bounded by half a step of the value range.
    step = (original.max() - original.min()) / ((1 << num_bit) - 1)
    assert np.abs(restored - original).max() <= step * 0.5 + 1e-4


@pytest.mark.parametrize("num_bit", [4, 8, 16])
def test_fewer_bits_means_a_smaller_message(num_bit):
    rng = np.random.default_rng(11)
    tensor = rng.normal(0, 1, (1, 32, 80, 80)).astype(np.float32)
    frame = codec.encode(tensor, num_bit, {})
    header = codec.header_only(frame)

    raw_fp32 = tensor.size * 4
    assert header["raw_bytes"] == raw_fp32
    assert header["wire_bytes"] < raw_fp32
    # Quantization alone bounds the payload; compression only helps further.
    assert header["wire_bytes"] <= raw_fp32 * num_bit / 32 + 1024


def test_header_only_avoids_decompression():
    tensor = np.zeros((1, 8, 8, 8), dtype=np.float32)
    frame = codec.encode(tensor, 8, {"frame_id": 42, "edge_ms": 1.5})
    header = codec.header_only(frame)
    assert header["frame_id"] == 42
    assert header["edge_ms"] == 1.5
    assert header["magic"] == codec.MAGIC


def test_constant_tensor_does_not_divide_by_zero():
    tensor = np.full((1, 4, 4, 4), 2.5, dtype=np.float32)
    restored, _ = codec.decode(codec.encode(tensor, 8, {}))
    assert np.allclose(restored, 2.5)


def test_rejects_out_of_range_bit_width():
    tensor = np.zeros((1, 2, 2, 2), dtype=np.float32)
    for bad in (0, 17, 32):
        with pytest.raises(ValueError):
            codec.encode(tensor, bad, {})


def test_rejects_corrupt_frames():
    with pytest.raises(ValueError):
        codec.decode(b"\x00\x00")
    tensor = np.zeros((1, 2, 2, 2), dtype=np.float32)
    good = codec.encode(tensor, 8, {})
    # Truncated header length.
    with pytest.raises(ValueError):
        codec.decode(good[:3])
    # Header claims a length past the end of the buffer.
    with pytest.raises(ValueError):
        codec.decode(b"\xff\xff\xff\xff" + good[4:])


def test_msg_size_tracks_the_simulator_estimate():
    """The simulator predicts `bytes * (nb/32) * 0.65`; the real codec should
    land in the same ballpark, otherwise live and simulated runs won't compare.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app.inference import simulation as sim

    # Smooth data, like a real activation map, rather than pure noise.
    xs = np.linspace(0, 6, 64, dtype=np.float32)
    tensor = np.tile(np.sin(xs)[None, None, None, :], (1, 32, 64, 1)).astype(np.float32)

    raw_bytes = tensor.size * 4
    predicted = sim.msg_mb(raw_bytes, 8)
    actual = codec.header_only(codec.encode(tensor, 8, {}))["wire_bytes"] / 1e6

    # The 0.65 factor is a stand-in for the compressor, so only the order of
    # magnitude is meaningful -- compressible data beats the estimate.
    assert 0 < actual <= predicted * 1.5
