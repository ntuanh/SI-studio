#!/usr/bin/env python3
"""Split a model into `head.pt` / `tail.pt` TorchScript shards.

    python tools/split_model.py --weights yolo11n.pt --cut 6 --imgsz 640
    python tools/split_model.py --weights yolo11n.pt --list-cuts

The agents load their shard with `torch.jit.load`, so no model class definition
has to exist on the devices.

A caveat worth knowing before you pick a cut
--------------------------------------------
YOLO is not a plain `Sequential`: neck/head layers consume activations from
several earlier layers (`Concat`, `C2f` shortcuts). A cut is only *clean* if no
layer at index >= cut reads an activation produced before index cut - 1 --
otherwise the tail would need tensors the edge never sent. `--list-cuts` reports
which indices are clean; splitting at a dirty one is refused rather than
silently producing a shard that fails at inference time.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

try:
    import torch
    from torch import nn
except ImportError:  # pragma: no cover
    sys.exit("this tool needs torch installed locally: pip install torch")


# --------------------------------------------------------------- graph checks
def _layer_sources(layer: nn.Module, index: int) -> list[int]:
    """Absolute indices this layer reads from (ultralytics `.f` convention)."""
    f = getattr(layer, "f", -1)
    refs = f if isinstance(f, (list, tuple)) else [f]
    return [(index - 1 if r == -1 else int(r)) for r in refs]


def analyze(layers: nn.Sequential) -> dict[int, list[tuple[int, int]]]:
    """cut -> list of (tail_layer_index, source_index) dependencies it breaks."""
    problems: dict[int, list[tuple[int, int]]] = {}
    for cut in range(1, len(layers)):
        broken = [
            (i, src)
            for i in range(cut, len(layers))
            for src in _layer_sources(layers[i], i)
            if src < cut - 1
        ]
        if broken:
            problems[cut] = broken
    return problems


# ------------------------------------------------------------------- wrappers
class Head(nn.Module):
    """Runs `layers[:cut]`, returning the activation to ship over the wire."""

    def __init__(self, layers: nn.Sequential) -> None:
        super().__init__()
        self.layers = layers

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        for layer in self.layers:
            x = layer(x)
        return x


class Tail(nn.Module):
    """Runs `layers[cut:]` on the received activation."""

    def __init__(self, layers: nn.Sequential) -> None:
        super().__init__()
        self.layers = layers

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        for layer in self.layers:
            x = layer(x)
        return x


# ----------------------------------------------------------------------- load
def load_layers(weights: str) -> tuple[nn.Sequential, str]:
    """Return the flat layer sequence plus a human label for the source."""
    path = Path(weights)
    if not path.is_file():
        sys.exit(f"weights not found: {path}")

    try:
        from ultralytics import YOLO

        yolo = YOLO(str(path))
        core = yolo.model
        seq = core.model if isinstance(getattr(core, "model", None), nn.Sequential) else core
        if not isinstance(seq, nn.Sequential):
            sys.exit("could not find a Sequential inside the ultralytics model")
        return seq.float().eval(), f"ultralytics:{path.name}"
    except ImportError:
        pass

    obj = torch.load(str(path), map_location="cpu", weights_only=False)
    module = obj.get("model", obj) if isinstance(obj, dict) else obj
    if isinstance(module, nn.Sequential):
        return module.float().eval(), f"sequential:{path.name}"
    inner = getattr(module, "model", None)
    if isinstance(inner, nn.Sequential):
        return inner.float().eval(), f"module.model:{path.name}"
    sys.exit(
        "unsupported checkpoint: expected an ultralytics YOLO or an nn.Sequential. "
        "Install ultralytics, or pre-export your own head/tail TorchScript modules."
    )


# ----------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description="Produce head.pt / tail.pt shards")
    ap.add_argument("--weights", required=True, help="e.g. yolo11n.pt")
    ap.add_argument("--cut", type=int, help="run layers[:cut] on the edge")
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--out", default="shards", help="output directory")
    ap.add_argument("--list-cuts", action="store_true", help="report clean cut indices and exit")
    ap.add_argument("--trace", action="store_true",
                    help="use torch.jit.trace instead of script (fallback for exotic modules)")
    args = ap.parse_args()

    layers, label = load_layers(args.weights)
    problems = analyze(layers)
    clean = [c for c in range(1, len(layers)) if c not in problems]

    if args.list_cuts:
        print(json.dumps(
            {
                "source": label,
                "layer_count": len(layers),
                "clean_cuts": clean,
                "dirty_cuts": {
                    str(c): [f"layer {i} reads layer {s}" for i, s in deps[:4]]
                    for c, deps in problems.items()
                },
            },
            indent=2,
        ))
        return 0

    if args.cut is None:
        ap.error("--cut is required unless --list-cuts is given")

    cut = args.cut
    if not 1 <= cut < len(layers):
        sys.exit(f"--cut must be in 1..{len(layers) - 1} (model has {len(layers)} layers)")
    if cut in problems:
        deps = ", ".join(f"layer {i} needs layer {s}" for i, s in problems[cut][:5])
        sys.exit(
            f"cut {cut} is not clean -- the tail would need activations the edge never sends "
            f"({deps}).\nClean cuts for this model: {clean}"
        )

    head = Head(nn.Sequential(*list(layers)[:cut])).eval()
    tail = Tail(nn.Sequential(*list(layers)[cut:])).eval()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    example = torch.zeros(1, 3, args.imgsz, args.imgsz)
    with torch.no_grad():
        fmap = head(example)
        tail(fmap)  # fail here rather than on the device

    def export(module: nn.Module, sample: torch.Tensor, dest: Path) -> None:
        if args.trace:
            scripted = torch.jit.trace(module, sample, strict=False)
        else:
            try:
                scripted = torch.jit.script(module)
            except Exception as exc:
                print(f"  script failed ({type(exc).__name__}); falling back to trace")
                scripted = torch.jit.trace(module, sample, strict=False)
        torch.jit.save(scripted, str(dest))

    print(f"source      : {label}")
    print(f"layers      : {len(layers)}  cut at {cut}")
    print(f"feature map : {tuple(fmap.shape)}  ({fmap.numel() * 4 / 1e6:.2f} MB fp32)")

    export(head, example, out_dir / "head.pt")
    export(tail, fmap, out_dir / "tail.pt")

    for name in ("head.pt", "tail.pt"):
        size = (out_dir / name).stat().st_size / 1e6
        print(f"wrote       : {out_dir / name}  ({size:.1f} MB)")

    print(
        "\nNext: POST /run/deploy (shards are read from SHARDS_DIR), "
        f"then set the cluster's cut_layer to {cut} if you want to pin it."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
