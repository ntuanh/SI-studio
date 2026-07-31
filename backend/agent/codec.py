"""Wire format for the intermediate feature map (edge -> cloud).

Frame layout
------------
    [4 bytes big-endian header length][UTF-8 JSON header][payload bytes]

The payload is the activation tensor quantized to `num_bit` bits, bit-packed,
then zlib-compressed. That combination is what the simulator's message-size
formula approximates:

    msg_mb = raw_bytes * (num_bit / 32) * 0.65 / 1e6

`(num_bit / 32)` is the quantization ratio; 0.65 stands in for the compressor.
The header carries the real `raw_bytes` and on-wire size so the control plane
reports measured message sizes rather than the estimate.

Imported by both agents; kept dependency-light (numpy only).
"""

from __future__ import annotations

import json
import struct
import zlib
from typing import Any

import numpy as np

_LEN = struct.Struct(">I")
MAGIC = "sifm1"  # split-inference feature map, v1


# --------------------------------------------------------------- quantization
def quantize(tensor: np.ndarray, num_bit: int) -> tuple[np.ndarray, float, float]:
    """Affine per-tensor quantization to `num_bit` unsigned levels.

    Returns (codes, scale, zero) where `dequantize(codes, scale, zero)` recovers
    an approximation of the input.
    """
    if not 1 <= num_bit <= 16:
        raise ValueError(f"num_bit must be in 1..16, got {num_bit}")

    arr = np.ascontiguousarray(tensor, dtype=np.float32)
    lo = float(arr.min()) if arr.size else 0.0
    hi = float(arr.max()) if arr.size else 0.0
    levels = (1 << num_bit) - 1
    scale = (hi - lo) / levels if hi > lo else 1.0

    codes = np.rint((arr - lo) / scale).astype(np.uint16)
    np.clip(codes, 0, levels, out=codes)
    return codes, scale, lo


def dequantize(codes: np.ndarray, scale: float, zero: float) -> np.ndarray:
    return codes.astype(np.float32) * np.float32(scale) + np.float32(zero)


def _pack(codes: np.ndarray, num_bit: int) -> bytes:
    """Bit-pack the codes. 8 and 16 bit are byte-aligned fast paths."""
    if num_bit == 8:
        return codes.astype(np.uint8).tobytes()
    if num_bit == 16:
        return codes.astype("<u2").tobytes()
    bits = np.unpackbits(codes.astype(">u2").view(np.uint8).reshape(-1, 2), axis=1)
    # Keep the low `num_bit` bits of each 16-bit code.
    return np.packbits(bits[:, 16 - num_bit:].reshape(-1)).tobytes()


def _unpack(blob: bytes, num_bit: int, count: int) -> np.ndarray:
    if num_bit == 8:
        return np.frombuffer(blob, dtype=np.uint8, count=count).astype(np.uint16)
    if num_bit == 16:
        return np.frombuffer(blob, dtype="<u2", count=count).astype(np.uint16)
    bits = np.unpackbits(np.frombuffer(blob, dtype=np.uint8))[: count * num_bit]
    rows = bits.reshape(count, num_bit)
    pad = np.zeros((count, 16 - num_bit), dtype=np.uint8)
    return np.packbits(np.hstack([pad, rows]), axis=1).view(">u2").reshape(-1).astype(np.uint16)


# ------------------------------------------------------------------- encoding
def encode(
    tensor: np.ndarray,
    num_bit: int,
    meta: dict[str, Any],
    *,
    compress_level: int = 6,
) -> bytes:
    codes, scale, zero = quantize(tensor, num_bit)
    packed = _pack(codes, num_bit)
    body = zlib.compress(packed, compress_level)

    header = {
        "magic": MAGIC,
        "shape": list(tensor.shape),
        "count": int(codes.size),
        "num_bit": num_bit,
        "scale": scale,
        "zero": zero,
        "raw_bytes": int(np.prod(tensor.shape)) * 4,  # fp32 equivalent
        "wire_bytes": len(body),
        **meta,
    }
    head = json.dumps(header).encode("utf-8")
    return _LEN.pack(len(head)) + head + body


def decode(frame: bytes) -> tuple[np.ndarray, dict[str, Any]]:
    if len(frame) < _LEN.size:
        raise ValueError("frame too short")
    (head_len,) = _LEN.unpack_from(frame, 0)
    start = _LEN.size + head_len
    if head_len <= 0 or start > len(frame):
        raise ValueError("frame header length out of range")

    header = json.loads(frame[_LEN.size : start].decode("utf-8"))
    if header.get("magic") != MAGIC:
        raise ValueError(f"unexpected magic {header.get('magic')!r}")

    packed = zlib.decompress(frame[start:])
    codes = _unpack(packed, int(header["num_bit"]), int(header["count"]))
    tensor = dequantize(codes, float(header["scale"]), float(header["zero"]))
    return tensor.reshape(tuple(header["shape"])), header


def header_only(frame: bytes) -> dict[str, Any]:
    """Read the header without paying for decompression."""
    (head_len,) = _LEN.unpack_from(frame, 0)
    return json.loads(frame[_LEN.size : _LEN.size + head_len].decode("utf-8"))
