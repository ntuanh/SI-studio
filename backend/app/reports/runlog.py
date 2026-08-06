"""Read a split-inference result directory as the shape it actually has.

`parse.py` is the fallback for a directory nobody has seen before: it hunts for
`name value` anywhere and groups the numbers **by the file they came from**.
That is the only thing it can do without a schema, and for these result
directories it is wrong in a way that shows up on screen -- every line here
carries a `cluster=`, a `role=` or a `kind=`, and throwing those away leaves the
file as the only dimension left to compare. The charts that came out said
things like "`clusters=2` in fps_cluster.log vs `clusters=2` in
utilization_cluster.log", which is not a comparison anybody asked for.

The result directory *does* have a schema -- `guides/server_results_guide.md`
documents all nine files line by line -- and `guides/visual_guide.md` §III.3
maps each of them to the chart it feeds. So this reads them as themselves:
cluster, role and kind become dimensions, and `charts.py` gets to plot the
comparison the run is actually about.

Conventions from the visual guide §III.2, applied here rather than downstream:

* every parser returns rows and `continue`s past lines it does not own, so a
  mixed-format file cannot raise;
* raw ids become display labels at parse time (`intermediate_queue_0` ->
  `Cluster 0`), so no chart code ever contains a queue name;
* units are normalised at the edge -- `%` stripped to a float, ms kept as ms
  and converted once at the axis.

`detect()` is deliberately strict about what counts as one of these
directories: a false positive would draw an empty catalogue instead of falling
back to the parser that would have found something.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from .window import Window

log = logging.getLogger(__name__)

#: The nine files of `server_results_guide.md` §2, plus the archived config.
FILES = (
    "batch_done_ns.log", "fps_cluster_ns.log", "fps_cluster.log",
    "utilization.log", "utilization_cluster.log", "latency_cluster.log",
    "map_window.log", "map.log", "cut_change_ns.log",
)

#: Enough of them present to be sure. `fps_cluster.log` carries the headline
#: throughput and `utilization*.log` the balance story; a directory with
#: neither is not one of these runs however many other names happen to match.
_REQUIRED = ("fps_cluster.log", "fps_cluster_ns.log", "batch_done_ns.log")

SYSTEM = "System"
OVERALL = "Overall"

_KV = re.compile(r"([A-Za-z][\w.]*)=([^\s]+)")
_NS = re.compile(r"^(\d{13,})\b")
#: `<ns> intermediate_queue_0: cut 11->12 deeper` -- matched against the body,
#: never the whole line: the ns timestamp is `\S+` too.
_CUT = re.compile(r"(\S+):\s*cut\s+(\d+)\s*->\s*(\d+)\s*(\w+)?")
_QUEUE = re.compile(r"^(?:intermediate_)?queue[_-]?(\d+)$", re.IGNORECASE)


def cluster_label(raw: str) -> str:
    """`intermediate_queue_0` -> `Cluster 0`; anything else keeps its own name."""
    m = _QUEUE.match((raw or "").strip())
    return f"Cluster {m.group(1)}" if m else (raw or "").strip()


def num(raw: str | None) -> float:
    """`'55.06%'` -> 55.06, `'336'` -> 336.0, anything else -> nan (guide §III.2)."""
    if raw is None:
        return math.nan
    try:
        return float(str(raw).strip().rstrip("%"))
    except ValueError:
        return math.nan


def _split(line: str) -> tuple[int | None, list[str], dict[str, str], str]:
    """`<ns> [FLAGS] k=v k=v` -> (ns, [FLAGS], {k: v}, body). The one line reader."""
    ts_match = _NS.match(line)
    ts = int(ts_match.group(1)) if ts_match else None
    body = line[ts_match.end():] if ts_match else line
    kv = {k: v for k, v in _KV.findall(body)}
    flags = [
        token for token in body.split()
        if "=" not in token and token.isupper() and token.isalpha()
    ]
    return ts, flags, kv, body


# --------------------------------------------------------------- data model
@dataclass(slots=True)
class Point:
    """One reading of a series: where it sits on the clock and in the order."""

    index: int
    at: float          # seconds from the run's first event
    value: float


@dataclass
class RunData:
    """Everything the nine files said, keyed by dimension rather than by file."""

    tag: str = ""
    clusters: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    #: ns-epoch of the run's first DONE. Every `at` below is seconds from here,
    #: so the cut markers and the FPS curve share one clock.
    base_ns: int | None = None

    #: fps_cluster.log — scope -> {fps, steady_fps, done, frames, share}
    throughput: dict[str, dict[str, float]] = field(default_factory=dict)
    #: batch_done_ns.log — the system-wide rolling window series
    system_fps: list[Point] = field(default_factory=list)
    #: fps_cluster_ns.log — cluster -> its own rolling window series
    cluster_fps: dict[str, list[Point]] = field(default_factory=dict)

    #: utilization_cluster.log — (scope, role) -> {utilization, …}. `role` is
    #: "all" on the rolled-up lines, which is what the guide's C7 x-axis is.
    utilization: dict[tuple[str, str], dict[str, float]] = field(default_factory=dict)
    #: utilization.log — one row per device
    devices: list[dict[str, object]] = field(default_factory=list)

    #: latency_cluster.log — (scope, role, kind) -> {n, mean_ms, p50_ms, …}
    latency: dict[tuple[str, str, str], dict[str, float]] = field(default_factory=dict)

    #: map.log — (scope, "WINDOW" | "ALL") -> {mAP50, mAP50_95}
    accuracy: dict[tuple[str, str], dict[str, float]] = field(default_factory=dict)
    #: map_window.log — cluster -> the sliding-window series
    accuracy_window: dict[str, list[dict[str, float]]] = field(default_factory=dict)

    #: cut_change_ns.log — adaptive split-point moves, seconds into the run
    cuts: list[dict[str, object]] = field(default_factory=list)
    #: config.yaml — the settings that produced these numbers
    config: dict[str, float] = field(default_factory=dict)

    #: The slice of the run the series above were cut to. The summary fields
    #: (`throughput`, `utilization`, `latency`, `accuracy`) are untouched by
    #: it — see `window.py` for why they cannot be.
    window: Window = field(default_factory=Window)

    warnings: list[str] = field(default_factory=list)

    # ---- accessors the chart code reads -------------------------------
    @property
    def scopes(self) -> list[str]:
        """Clusters in file order, then System. The narrative order for an axis."""
        return [*self.clusters, SYSTEM]

    @property
    def roles(self) -> list[str]:
        """`cloud` before `edge`: the order the pipeline runs in, not alphabetical."""
        seen = {role for _, role in self.utilization if role != "all"}
        seen |= {str(d["role"]) for d in self.devices}
        return [r for r in ("cloud", "edge") if r in seen] + sorted(seen - {"cloud", "edge"})

    @property
    def has_accuracy(self) -> bool:
        return bool(self.accuracy or self.accuracy_window)

    def system(self, field_: str) -> float:
        return self.throughput.get(SYSTEM, {}).get(field_, math.nan)


# ------------------------------------------------------------- file readers
def _lines(root: Path, name: str) -> list[str]:
    path = root / name
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log.warning("unreadable %s: %s", name, exc)
        return []
    return [ln for ln in (line.rstrip("\n") for line in text.splitlines()) if ln.strip()]


def _series(rows: list[tuple[int, float]]) -> list[Point]:
    """(ns, value) -> points measured in seconds from the first reading."""
    if not rows:
        return []
    base = rows[0][0]
    return [
        Point(index=i, at=(ts - base) / 1e9, value=value)
        for i, (ts, value) in enumerate(rows)
    ]


def read_throughput(root: Path, data: RunData) -> None:
    """`fps_cluster.log` §2.3 — one line per cluster plus `SYSTEM`."""
    for line in _lines(root, "fps_cluster.log"):
        _, flags, kv, _body = _split(line)
        scope = SYSTEM if "SYSTEM" in flags else cluster_label(kv.get("cluster", ""))
        if not scope:
            continue
        if scope != SYSTEM and scope not in data.clusters:
            data.clusters.append(scope)
        data.throughput[scope] = {
            key: num(kv.get(key))
            for key in ("fps", "steady_fps", "done", "frames", "share", "clusters")
            if key in kv
        }


def read_system_fps(root: Path, data: RunData) -> None:
    """`batch_done_ns.log` §2.1 — `<ns>` while warming up, `<ns> <fps>` after.

    The bare first-15 lines are exactly the positional shape the visual guide
    §III.2 warns about: they are real events with no reading yet, so they set
    the clock and contribute no point.
    """
    rows: list[tuple[int, float]] = []
    base: int | None = None
    for line in _lines(root, "batch_done_ns.log"):
        parts = line.split()
        if not parts or not parts[0].isdigit():
            continue
        ts = int(parts[0])
        if base is None:
            base = ts
        if len(parts) >= 2:
            value = num(parts[1])
            if not math.isnan(value):
                rows.append((ts, value))
    if base is not None:
        data.base_ns = base
    if rows and base is not None:
        rows.insert(0, (base, math.nan))          # anchor t=0 at the first DONE
        data.system_fps = [p for p in _series(rows) if not math.isnan(p.value)]


def read_cluster_fps(root: Path, data: RunData) -> None:
    """`fps_cluster_ns.log` §2.2 — the same arrivals, bucketed by cluster."""
    rows: dict[str, list[tuple[int, float]]] = {}
    base: int | None = None
    for line in _lines(root, "fps_cluster_ns.log"):
        ts, _, kv, _body = _split(line)
        if ts is None:
            continue
        if base is None:
            base = ts
        cluster = cluster_label(kv.get("cluster", ""))
        if not cluster or "window_fps" not in kv:
            continue
        value = num(kv["window_fps"])
        if not math.isnan(value):
            rows.setdefault(cluster, []).append((ts, value))

    for cluster, samples in rows.items():
        if base is not None:
            samples.insert(0, (base, math.nan))   # every cluster shares one clock
        data.cluster_fps[cluster] = [p for p in _series(samples) if not math.isnan(p.value)]
        if cluster not in data.clusters:
            data.clusters.append(cluster)


def read_utilization(root: Path, data: RunData) -> None:
    """`utilization_cluster.log` §2.5 and `utilization.log` §2.4."""
    for line in _lines(root, "utilization_cluster.log"):
        _, flags, kv, _body = _split(line)
        scope = SYSTEM if "SYSTEM" in flags else cluster_label(kv.get("cluster", ""))
        if not scope:
            continue
        # `ALL` is a flag on the rolled-up cluster line; a role line has `role=`.
        role = kv.get("role", "all")
        data.utilization[(scope, role)] = {
            key: num(kv.get(key))
            for key in ("utilization", "utilization_mean", "devices", "busy_s",
                        "total_s", "packages")
            if key in kv
        }

    for line in _lines(root, "utilization.log"):
        _, _, kv, _body = _split(line)
        if "utilization" not in kv:
            continue
        data.devices.append({
            "client": kv.get("client", ""),
            "role": kv.get("role", "unknown"),
            "packages": num(kv.get("packages")),
            "busy_s": num(kv.get("busy_s")),
            "total_s": num(kv.get("total_s")),
            "utilization": num(kv["utilization"]),
        })


def read_latency(root: Path, data: RunData) -> None:
    """`latency_cluster.log` §2.6 — service is per role, e2e is per cluster."""
    for line in _lines(root, "latency_cluster.log"):
        _, flags, kv, _body = _split(line)
        scope = SYSTEM if "SYSTEM" in flags else cluster_label(kv.get("cluster", ""))
        kind = kv.get("kind", "")
        if not scope or not kind:
            continue
        data.latency[(scope, kv.get("role", "all"), kind)] = {
            key: num(kv.get(key))
            for key in ("n", "mean_ms", "p50_ms", "p95_ms", "max_ms")
            if key in kv
        }


def read_accuracy(root: Path, data: RunData) -> None:
    """`map.log` §2.8 (two lines per scope) and `map_window.log` §2.7."""
    for line in _lines(root, "map.log"):
        _, flags, kv, _body = _split(line)
        scope = OVERALL if "OVERALL" in flags else cluster_label(kv.get("cluster", ""))
        agg = next((f for f in flags if f in ("WINDOW", "ALL")), "")
        if not scope or not agg:
            continue
        data.accuracy[(scope, agg)] = {
            "mAP50": num(kv.get("mAP50")), "mAP50_95": num(kv.get("mAP50_95")),
        }

    for line in _lines(root, "map_window.log"):
        _, _, kv, _body = _split(line)
        cluster = cluster_label(kv.get("cluster", ""))
        if not cluster or "window" not in kv:
            continue
        data.accuracy_window.setdefault(cluster, []).append({
            "window": num(kv["window"]),
            "frames": num(kv.get("frames")),
            "mAP50": num(kv.get("mAP50")),
            "mAP50_95": num(kv.get("mAP50_95")),
        })
    for rows in data.accuracy_window.values():
        rows.sort(key=lambda r: r["window"])


def read_cuts(root: Path, data: RunData) -> None:
    """`cut_change_ns.log` §2.9 — adaptive runs only, and stale in other tags.

    The guide is explicit that a `split` archive can carry a previous dynamic
    run's file, so this is only read when the tag says the controller was on.
    """
    if data.tag and data.tag != "dynamic":
        return
    base = data.base_ns
    for line in _lines(root, "cut_change_ns.log"):
        ts, _, _, body = _split(line)
        m = _CUT.search(body)
        if ts is None or not m:
            continue
        data.cuts.append({
            "at": (ts - base) / 1e9 if base is not None else math.nan,
            "cluster": cluster_label(m.group(1)),
            "from": int(m.group(2)), "to": int(m.group(3)),
            "direction": (m.group(4) or "").lower(),
        })


def read_config(root: Path, data: RunData) -> None:
    """`config.yaml` — the archived settings. A scalar reader, not a YAML parser.

    Only `key: <number>` is wanted, and depending on PyYAML for that would put
    a package in `requirements.txt` for one dictionary.
    """
    for line in _lines(root, "config.yaml"):
        if line.lstrip().startswith("#") or ":" not in line:
            continue
        key, _, raw = line.partition(":")
        key, raw = key.strip(), raw.split("#")[0].strip()
        if not key or not raw:
            continue
        value = num(raw.strip("'\""))
        if not math.isnan(value):
            data.config[key] = value
        elif key == "tag" and not data.tag:
            data.tag = raw.strip("'\"")


# --------------------------------------------------------------- entry point
def detect(root: Path) -> bool:
    """Is this one of the server's result directories?

    Strict on purpose: guessing wrong draws an empty catalogue where the
    generic parser would have found something real.
    """
    try:
        names = {p.name for p in root.iterdir() if p.is_file()}
    except OSError:
        return False
    return sum(name in names for name in _REQUIRED) >= 2


def tag_of(root: Path) -> str:
    """`results_0729_2204_dynamic` -> `dynamic` (§3's archive naming)."""
    parts = root.name.rsplit("_", 1)
    tail = parts[-1].lower() if len(parts) > 1 else ""
    return tail if tail in ("dynamic", "split", "only_cloud", "only_edge") else ""


def apply_window(data: RunData, window: Window) -> RunData:
    """Cut every per-batch series down to `window`, in place.

    Each series is sliced on its own length rather than on a shared clock: they
    are all one-reading-per-batch, so the same percentage lands at the same
    point in the run for each of them, and a cluster that produced fewer
    batches is still cut at *its* 5% mark rather than at another cluster's.

    `cuts` is the exception, because a split-point change is an event at an
    arbitrary moment rather than a reading. Those are cut on the clock, to the
    span the kept readings actually cover — a marker for a change that happened
    outside the window would be drawn at the edge of the axes and read as
    having happened inside it.
    """
    data.window = window
    if window.whole:
        return data

    data.system_fps = window.clip(data.system_fps)
    data.cluster_fps = {c: window.clip(points) for c, points in data.cluster_fps.items()}
    data.accuracy_window = {
        c: window.clip(rows) for c, rows in data.accuracy_window.items()
    }

    kept = [p for points in (data.system_fps, *data.cluster_fps.values()) for p in points]
    if kept and data.cuts:
        first, last = min(p.at for p in kept), max(p.at for p in kept)
        data.cuts = [
            cut for cut in data.cuts
            if math.isfinite(float(cut.get("at", math.nan)))
            and first <= float(cut["at"]) <= last
        ]
    return data


def read_run(root: Path, window: Window | None = None) -> RunData:
    """Read every file the run wrote. One bad file never sinks the report."""
    data = RunData(tag=tag_of(root))
    data.files = sorted(p.name for p in root.iterdir() if p.is_file())

    for reader in (read_config, read_throughput, read_system_fps, read_cluster_fps,
                   read_utilization, read_latency, read_accuracy, read_cuts):
        try:
            reader(root, data)
        except Exception as exc:  # noqa: BLE001 - a broken log is not a broken report
            log.warning("%s failed on %s: %s", reader.__name__, root, exc)
            data.warnings.append(f"{reader.__name__.removeprefix('read_')}: {exc}")

    # Clusters arrive from whichever file was read first; keep them in the
    # order the numbers suffix implies so "Cluster 0" is always the left bar.
    data.clusters.sort(key=lambda c: (len(c), c))

    missing = [name for name in FILES if name not in data.files]
    if missing and data.tag != "dynamic":
        missing = [m for m in missing if m != "cut_change_ns.log"]
    if missing:
        data.warnings.append("not written by this run: " + ", ".join(missing))

    # Last, so the window cuts the finished series rather than racing the
    # readers that are still appending to them.
    return apply_window(data, window or Window())
