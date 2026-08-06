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


def _batch_range(raw: str | None) -> tuple[float, float]:
    """`'12-27'` -> (12.0, 27.0). `nan`s when the field is missing or odd."""
    first, _, last = (raw or "").partition("-")
    lo, hi = num(first), num(last)
    return (lo, hi) if math.isfinite(lo) and math.isfinite(hi) else (math.nan, math.nan)


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

    #: Every batch completion, in seconds from the first one — including the
    #: warm-up batches that carry no rolling FPS yet. This is the raw event
    #: stream `throughput` is a summary *of*, and the reason a window can
    #: recompute that summary rather than only relabel it.
    system_batches: list[float] = field(default_factory=list)
    #: The same events split by cluster (`fps_cluster_ns.log`).
    cluster_batches: dict[str, list[float]] = field(default_factory=dict)
    #: When the run ended, in seconds from its first batch. The full system
    #: span every window percentage is measured against.
    span_s: float = 0.0

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

    #: The slice of the run everything above was cut to, and the two moments
    #: (seconds from the first batch) it worked out to on this run's clock.
    window: Window = field(default_factory=Window)
    window_span: tuple[float, float] | None = None
    #: How long each series was *before* the window cut it, keyed `"system"`
    #: and by cluster. Smoothing widths are chosen from these, so two windows
    #: of one run draw the same curve wherever they overlap.
    full_counts: dict[str, int] = field(default_factory=dict)
    #: Which summaries the window worked out again from the raw events, rather
    #: than inherited from the run's own totals. A chart is only labelled
    #: "recomputed" if its name is in here, so a run that did not write the
    #: events cannot end up with a whole-run number wearing a window's label.
    recomputed: set[str] = field(default_factory=set)

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

    @property
    def batch_size(self) -> float:
        """Frames per batch. `config.yaml` if it says, else what the run counted.

        The archived config writes it as `batch-size` under `server:`, and some
        runs do not archive a config at all -- but `fps_cluster.log` always
        carries `frames` and `done`, and their ratio is the same number.
        """
        for key in ("batch_size", "batch-size"):
            size = self.config.get(key, math.nan)
            if math.isfinite(size) and size > 0:
                return size
        for stats in self.throughput.values():
            frames, done = stats.get("frames", math.nan), stats.get("done", math.nan)
            if math.isfinite(frames) and math.isfinite(done) and done > 0:
                return frames / done
        return math.nan


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
    events: list[int] = []
    base: int | None = None
    for line in _lines(root, "batch_done_ns.log"):
        parts = line.split()
        if not parts or not parts[0].isdigit():
            continue
        ts = int(parts[0])
        if base is None:
            base = ts
        events.append(ts)
        if len(parts) >= 2:
            value = num(parts[1])
            if not math.isnan(value):
                rows.append((ts, value))
    if base is not None:
        data.base_ns = base
        # Every completion, warm-up included. The rolling-FPS column starts
        # fifteen batches in, but the run's clock starts at the first DONE --
        # and it is that clock the window's percentages are measured against.
        data.system_batches = [(ts - base) / 1e9 for ts in events]
        data.span_s = data.system_batches[-1] if data.system_batches else 0.0
    if rows and base is not None:
        rows.insert(0, (base, math.nan))          # anchor t=0 at the first DONE
        data.system_fps = [p for p in _series(rows) if not math.isnan(p.value)]


def read_cluster_fps(root: Path, data: RunData) -> None:
    """`fps_cluster_ns.log` §2.2 — the same arrivals, bucketed by cluster."""
    rows: dict[str, list[tuple[int, float]]] = {}
    events: dict[str, list[int]] = {}
    base: int | None = None
    for line in _lines(root, "fps_cluster_ns.log"):
        ts, _, kv, _body = _split(line)
        if ts is None:
            continue
        if base is None:
            base = ts
        cluster = cluster_label(kv.get("cluster", ""))
        if not cluster:
            continue
        # Every arrival, whether or not it carries a window reading yet: this
        # is what a windowed throughput is counted from.
        events.setdefault(cluster, []).append(ts)
        if "window_fps" not in kv:
            continue
        value = num(kv["window_fps"])
        if not math.isnan(value):
            rows.setdefault(cluster, []).append((ts, value))

    # The system clock, so a cluster's events land on the same axis as the
    # system's. `base_ns` is set by `read_system_fps`, which runs first.
    clock = data.base_ns if data.base_ns is not None else base
    for cluster, stamps in events.items():
        if clock is not None:
            data.cluster_batches[cluster] = [(ts - clock) / 1e9 for ts in stamps]
        if cluster not in data.clusters:
            data.clusters.append(cluster)

    for cluster, samples in rows.items():
        # Measured from the system's first DONE rather than from this file's
        # own first line, so a cluster series, a cut marker and the window's
        # own two moments are all points on one axis.
        origin = clock if clock is not None else samples[0][0]
        data.cluster_fps[cluster] = [
            Point(index=i, at=(ts - origin) / 1e9, value=value)
            for i, (ts, value) in enumerate(samples)
        ]
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
        # `batches=12-27` is the only thing on this line that says *when* the
        # window happened; without it a time window has no way to place it.
        first, last = _batch_range(kv.get("batches"))
        data.accuracy_window.setdefault(cluster, []).append({
            "window": num(kv["window"]),
            "frames": num(kv.get("frames")),
            "first_batch": first,
            "last_batch": last,
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


#: What a window cannot touch, and why. These files hold one finished total
#: each -- `busy_s`/`total_s` accumulated per device, latency already reduced
#: to mean/p50/p95/max, mAP already matched frame by frame -- and the run did
#: not write the per-batch series any of them were summed from. There is no
#: arithmetic here that recovers the utilization of batches 25-478 from a
#: single cumulative busy time, so those figures stay whole-run and say so.
WHOLE_RUN_ONLY = (
    "device utilization (only a cumulative busy/total per device was written)",
    "service, pipeline and end-to-end latency (only n/mean/p50/p95/max)",
    "accuracy over all frames (only the finished per-frame match)",
)


def recompute(data: RunData, span: tuple[float, float]) -> None:
    """Redo the throughput and windowed-accuracy summaries over `span`.

    The same arithmetic the run itself used, applied to the events inside the
    window instead of to all of them:

    * **throughput** — count the batch completions between the two moments,
      multiply by the batch size for frames, divide by the span's length. On a
      whole run this reproduces the run's own `steady_fps` (frames after the
      first DONE over the elapsed time), which is the check that the recipe is
      the right one.
    * **accuracy WINDOW** — `map.log`'s own note says it is "mean of N
      window(s)", so the mean of the windows left inside the span *is* that
      figure computed for the span.

    `steady_fps` is dropped rather than recomputed: it exists to name the rate
    with the warm-up taken off, and a window that excluded the warm-up has
    already done that. Two bars meaning the same thing is a comparison that is
    not in the data.
    """
    low, high = span
    elapsed = high - low
    size = data.batch_size
    counts = {
        cluster: sum(1 for at in stamps if low < at <= high)
        for cluster, stamps in data.cluster_batches.items()
    }
    system_done = sum(1 for at in data.system_batches if low < at <= high)
    # A run with one cluster writes the same events to both files; prefer the
    # system log, and fall back to the clusters when it was not written.
    total = system_done or sum(counts.values())

    # No events, no arithmetic. Leaving the run's own totals in place is the
    # honest outcome -- they are then labelled whole-run like the rest, rather
    # than a stale figure wearing the window's name.
    if elapsed > 0 and math.isfinite(size) and total:
        for cluster, done in counts.items():
            stats = dict(data.throughput.get(cluster, {}))
            stats.pop("steady_fps", None)
            stats.update(done=float(done), frames=done * size,
                         fps=done * size / elapsed,
                         share=done / total * 100.0)
            data.throughput[cluster] = stats

        stats = dict(data.throughput.get(SYSTEM, {}))
        stats.pop("steady_fps", None)
        stats.update(done=float(total), frames=total * size,
                     fps=total * size / elapsed)
        data.throughput[SYSTEM] = stats
        data.recomputed.add("throughput")

    # `map.log`'s WINDOW row says of itself "mean of N window(s)", so the mean
    # over the windows left inside the span is that same figure for the span.
    # The rows have already been cut to the span by the caller.
    for cluster, rows in data.accuracy_window.items():
        means = {
            key: [r[key] for r in rows if math.isfinite(r.get(key, math.nan))]
            for key in ("mAP50", "mAP50_95")
        }
        if all(means.values()):
            data.accuracy[(cluster, "WINDOW")] = {
                key: sum(values) / len(values) for key, values in means.items()
            }
            data.recomputed.add("accuracy_window")
        else:
            # No window survived: the run scored no frames in this stretch, and
            # a stale whole-run figure under a windowed heading would be a lie.
            data.accuracy.pop((cluster, "WINDOW"), None)

    scored = [
        data.accuracy[(c, "WINDOW")] for c in data.accuracy_window
        if (c, "WINDOW") in data.accuracy
    ]
    if (OVERALL, "WINDOW") in data.accuracy:
        if scored:
            data.accuracy[(OVERALL, "WINDOW")] = {
                key: sum(s[key] for s in scored) / len(scored)
                for key in ("mAP50", "mAP50_95")
            }
        else:
            data.accuracy.pop((OVERALL, "WINDOW"), None)


def apply_window(data: RunData, window: Window) -> RunData:
    """Cut the run to `window` and recompute what the events support, in place.

    One span, taken from the system's own clock, judges everything: the FPS
    series, each cluster's series, the split-point markers and the mAP windows.
    Cutting each series at its own 5% mark instead would hand the clusters
    different stretches of the run, and comparing those is the one thing the
    charts must not do.
    """
    # Recorded whether or not a window follows, so an unwindowed report and a
    # windowed one of the same run agree about how hard to smooth.
    data.full_counts = {"system": len(data.system_fps)}
    data.full_counts.update({c: len(p) for c, p in data.cluster_fps.items()})

    data.window = window
    if window.whole:
        return data

    # The window is a span of the system's clock, so without one there is
    # nothing to measure it against. Refusing beats guessing: cutting on a
    # made-up span would drop most of the run and say nothing about why.
    if data.span_s <= 0:
        latest = max((s[-1] for s in data.cluster_batches.values() if s), default=0.0)
        data.span_s = latest
    if data.span_s <= 0:
        data.window = Window()
        data.warnings.append(
            f"the {window.label} window was not applied: this run wrote no "
            "batch timestamps, so it has no clock to measure a window on"
        )
        return data

    span = window.span(0.0, data.span_s)
    data.window_span = span

    def inside(at: float) -> bool:
        return window.holds(at, span)

    # mAP's sliding windows are numbered by batch, not stamped with a time, so
    # they are placed on the clock through the completion times of the batches
    # they cover. That has to happen before the completions themselves are cut.
    before = {c: len(rows) for c, rows in data.accuracy_window.items()}
    data.accuracy_window = {
        c: [r for r in rows if _scored_inside(data.system_batches, r, span)]
        for c, rows in data.accuracy_window.items()
    }
    _warn_about_lost_scoring(data, before, span)

    data.system_fps = [p for p in data.system_fps if inside(p.at)]
    data.cluster_fps = {
        c: [p for p in points if inside(p.at)] for c, points in data.cluster_fps.items()
    }
    data.system_batches = [at for at in data.system_batches if inside(at)]
    data.cluster_batches = {
        c: [at for at in stamps if inside(at)] for c, stamps in data.cluster_batches.items()
    }
    data.cuts = [c for c in data.cuts if inside(float(c.get("at", math.nan)))]

    recompute(data, span)
    return data


def _warn_about_lost_scoring(
    data: RunData, before: dict[str, int], span: tuple[float, float]
) -> None:
    """Say when a window fell outside where the run actually scored accuracy.

    mAP is only computed over frames that have ground truth, and a run can
    carry those for a small stretch near the start -- 14 windows over batches
    0-28 of 504 is a real example. A window past that stretch legitimately
    keeps none of them, and the accuracy charts then do not draw at all. That
    is a fact about the run worth a sentence, not a chart quietly missing.
    """
    for cluster, had in before.items():
        kept = len(data.accuracy_window.get(cluster, []))
        if not had or kept == had:
            continue
        note = (
            f"{cluster}: {had - kept} of {had} mAP scoring window(s) fall "
            f"outside {data.window.label} ({span[0]:,.0f}s–{span[1]:,.0f}s)"
        )
        if not kept:
            note += (
                " — none are left, so the accuracy charts are not drawn. This "
                "run only scored frames outside the window; mAP needs ground "
                "truth, and it covers whichever batches had it"
            )
        data.warnings.append(note)


def _scored_inside(
    batches: list[float], row: dict[str, float], span: tuple[float, float]
) -> bool:
    """Does this mAP window's batch range sit wholly inside the time span?

    Both ends must be in, not merely the start: a window straddling the edge
    averages frames from outside the span, and folding that into a windowed
    mAP would put data the operator excluded back into the headline.

    Batch *k* finished at the *k*-th completion the run recorded, which is the
    bridge from `batches=12-27` to two moments on the clock.
    """
    first, last = row.get("first_batch", math.nan), row.get("last_batch", math.nan)
    if not (math.isfinite(first) and math.isfinite(last)):
        return False
    lo, hi = int(first), int(last)
    if not batches or lo < 0 or hi >= len(batches):
        return False
    return span[0] < batches[lo] and batches[hi] <= span[1]


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
