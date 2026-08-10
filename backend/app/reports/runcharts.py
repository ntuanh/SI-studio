"""The visual guide's Part II catalogue, drawn from a split-inference run.

`guides/visual_guide.md` §III.3 maps each result file to the chart it feeds, and
Part II gives the recipe for each of those charts. This is that table, in that
order:

| Input file | Feeds |
|---|---|
| `fps_cluster.log` | 01 throughput by cluster (C1) |
| `batch_done_ns.log` | 02 system window FPS over the run (C3) |
| `fps_cluster_ns.log` | 03 per-cluster window FPS (C2/C3), 04 spread (C4) |
| `latency_cluster.log` | 05 service latency by role (C5), 06 e2e profile (C6) |
| `utilization_cluster.log` | 07 utilization by cluster and role (C7) |
| `utilization.log` | 08 per-device utilization (C8) |
| `map_window.log` | 09 accuracy by window (C9) |
| `map.log` | 10 accuracy summary (C10) |
| `latency_cluster.log` | 11 pipeline against service — the queue wait |
| `free_time.log` | 12 free time against utilization, per device |
| `free_time_cluster.log` | 13 why it was free / where the busy went, 14 per machine |
| `free_time_series.log` | 15 free time over the run |
| `broker_ram_ns.log` | 16 the queue host's RAM against throughput |
| `broker_ram.log` | 17 the RAM profile and what the run added |
| *(derived)* | the stat tiles (C12) |

01-10 are the story every run tells. 11-17 are the diagnostics: one latency
comparison the first ten do not draw, and the two optional features
(§2.10-2.14) a run either measures or does not.

Chart numbers are fixed rather than sequential. A run with no `map.log` leaves
the 09/10 gap instead of renumbering, so a note saved against "07" keeps
meaning the utilization chart (§III.5).

Every function returns `None` when its file is missing or held nothing worth
drawing: a run that skipped mAP gets eight charts, not eight charts and two
empty frames.

Every function also takes a `View` -- the wording and the hidden set an operator
chose in the UI -- and declares, through `Shown`, everything it *can* draw. The
two are deliberately one mechanism: the config panel can only offer a series the
chart body actually knows how to switch off, and colors keep coming from entity
dicts so hiding one cluster cannot repaint the other (§1).
"""

from __future__ import annotations

import logging
import math
from typing import Callable, Sequence

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import LinearSegmentedColormap

from . import palette as pal
from .runlog import OVERALL, SYSTEM, WHOLE_RUN_ONLY, Point, RunData, num
from .style import (
    AXIS, INK_2, LINE_KW, MARKER_MAX, MARK_KW, MUTED, S1, S2, S3, SURFACE,
    TINT, BAR_KW, Canvas, Chart, Shown, Tile, View, band, entity_colors, fmt,
    applied, grouped_x, headroom, label_bars, offsets, panel_legend,
    rolling_mean, stable_smooth, suptitle, takeaway, tidy,
)
from .window import Window

log = logging.getLogger(__name__)

#: Cloud before edge: the order the pipeline runs in, not alphabetical.
ROLE_COLOR = {"cloud": S1, "edge": S2, "all": MUTED}
ROLE_LABEL = {"cloud": "Cloud", "edge": "Edge", "all": "All devices"}

#: Standard grouped-bar geometry (Part II preamble).
WIDTH = 0.36


def _cluster_colors(data: RunData) -> dict[str, str]:
    """`{cluster: color}` — an entity dict, so filtering never repaints (§1)."""
    return entity_colors(data.clusters[: len(pal.SLOTS_LIGHT)])


def _xy(points: Sequence[Point]) -> tuple[np.ndarray, np.ndarray]:
    return (np.array([p.at for p in points], dtype=float),
            np.array([p.value for p in points], dtype=float))


def _role_ticks(devices: Sequence[dict[str, object]]) -> list[str]:
    """`C1 C2 E1 E2` — numbered WITHIN each role.

    A running counter gives `C1 C2 E3 E4`, which reads as two missing devices
    (§C8).
    """
    seen: dict[str, int] = {}
    ticks: list[str] = []
    for device in devices:
        role = str(device["role"])
        seen[role] = seen.get(role, 0) + 1
        ticks.append(f"{role[:1].upper()}{seen[role]}")
    return ticks


def _endpoint(ax: plt.Axes, x: float, y: float, color: str, text: str) -> None:
    """Direct-label the endpoint — the one label a reader is trying to name (§1).

    An endpoint label may take the series color: it *is* the identity cue.
    """
    ax.plot([x], [y], "o", color=color, **MARK_KW)
    ax.annotate(text, xy=(x, y), xytext=(7, 0), textcoords="offset points",
                va="center", fontsize=9.5, fontweight="bold", color=color)


# --------------------------------------------------------- 01 · throughput
def throughput_by_cluster(canvas: Canvas, data: RunData, view: View) -> Chart | None:
    """C1 — grouped bar: whole-run rate against the warm-up-free rate.

    `fps` is additive (the clusters sum to SYSTEM) and `steady_fps` drops the
    warm-up, which is the fair number for comparing clusters (§2.3). Both, side
    by side, is the one chart that says whether the assignment balanced.
    """
    scopes = [s for s in data.scopes if s in data.throughput]
    if not scopes:
        return None

    shown = Shown(view, [("fps", "Whole run", S1), ("steady_fps", "Steady state", S2)])
    rows = [[data.throughput[s].get(ref.key, math.nan) for s in scopes] for ref in shown]
    # SYSTEM carries no steady-state figure, so its single bar sits on the tick
    # instead of parked in a slot beside an empty one.
    positions = grouped_x(rows, WIDTH)
    fig, ax = plt.subplots(figsize=(max(7.0, 1.9 * len(scopes) + 3.4), 4.3))

    heights: list[float] = []
    for i, ref in enumerate(shown):
        heights.extend(rows[i])
        bars = ax.bar(positions[i], rows[i], WIDTH, label=ref.label,
                      color=ref.color, **BAR_KW)
        label_bars(ax, bars, rows[i], "{:.1f}")

    title, ylabel = "Throughput by cluster", "Throughput (FPS)"
    ax.set_xticks(np.arange(len(scopes), dtype=float), scopes)
    view.label_axes(ax, y=ylabel)
    headroom(ax, heights)
    ax.set_title(view.titled(title))
    tidy(ax, categorical="x")
    if len(shown) > 1:
        ax.legend(loc="upper left", ncol=len(shown))

    shares = {s: data.throughput[s].get("share", math.nan)
              for s in data.clusters if s in data.throughput}
    live = {s: v for s, v in shares.items() if math.isfinite(v)}
    if len(live) >= 2:
        top = max(live, key=live.get)
        note = (f"{top} took {live[top]:.0f}% of all batches — "
                f"{'balanced' if max(live.values()) - min(live.values()) < 15 else 'uneven'} split")
    else:
        note = f"system throughput {fmt(data.system('fps'))} FPS"
    takeaway(ax, note)

    return canvas.save(fig, applied(Chart(
        id="throughput_by_cluster", file="", kind="comparison", title=title,
        subtitle="SYSTEM carries no steady-state figure — it is a per-cluster measure",
        summary="; ".join(
            f"{s} {fmt(data.throughput[s].get('fps'))} FPS"
            + (f" ({data.throughput[s]['share']:.0f}% of batches)"
               if math.isfinite(data.throughput[s].get("share", math.nan)) else "")
            for s in scopes
        ),
        metrics=["fps", "steady_fps", "share"]),
        view, title=title, ylabel=ylabel, shown=shown), index=1)


# ------------------------------------------------- 02 · system FPS timeline
def system_fps_timeline(canvas: Canvas, data: RunData, view: View) -> Chart | None:
    """C3 — the system-wide rolling window FPS, with the mean as a reference.

    The raw readings wear the pale end of the *same* sequential ramp rather
    than a faded slot color: `alpha` on a line shifts its hue toward the
    surface and breaks the contrast check the palette was validated against.
    """
    points = data.system_fps
    if len(points) < 8:
        return None
    x, y = _xy(points)
    mean = float(np.mean(y))
    window = stable_smooth(len(y), data.full_counts.get("system", 0))

    shown = Shown(view, [
        ("reading", "Reading", TINT),
        *([("mean", f"{window}-reading mean", S1)] if window else []),
        ("average", f"Run mean ({mean:.1f})", S1),
        *([("cuts", f"Split-point changes ({len(data.cuts)})", MUTED)] if data.cuts else []),
    ])

    fig, ax = plt.subplots(figsize=(11.5, 4.3))
    last = float(y[-1])
    if shown.has("reading"):
        ax.plot(x, y, color=TINT, label="reading" if window else "window FPS",
                linewidth=1.4 if window else 2.0, solid_capstyle="round")
    if shown.has("mean"):
        smoothed = rolling_mean(y, window)
        ax.plot(x, smoothed, color=S1, label=f"{window}-reading mean", **LINE_KW)
        last = float(smoothed[-1])
    elif not shown.has("reading"):
        # Both curves off would leave an empty frame; the raw series is the data.
        ax.plot(x, y, color=S1, label="window FPS", **LINE_KW)

    if shown.has("average"):
        # Reference line: same hue, thinner, receding (§C3).
        ax.axhline(mean, color=S1, linewidth=1.0, alpha=0.45)
        ax.annotate(f"mean {mean:.1f}", xy=(0.008, mean),
                    xycoords=("axes fraction", "data"), xytext=(0, 5),
                    textcoords="offset points", fontsize=9, color=INK_2)

    # Adaptive runs move the split point mid-run; the guide's §7 recipe is to
    # read accuracy and throughput against those moments, so they are marked.
    if shown.has("cuts"):
        for cut in data.cuts:
            at = float(cut.get("at", math.nan))
            if not math.isfinite(at) or not (x[0] <= at <= x[-1]):
                continue
            ax.axvline(at, color=AXIS, linewidth=1.0)
            # Inside the axes, not above them: a cut late in a long run would
            # otherwise put its label through the title.
            ax.annotate(f"cut {cut['from']}→{cut['to']}", xy=(at, 0.995),
                        xycoords=("data", "axes fraction"), xytext=(3, -2),
                        textcoords="offset points", ha="left", va="top",
                        fontsize=8.5, color=MUTED)

    _endpoint(ax, float(x[-1]), last, S1, f"{last:.1f}")
    title = "System throughput over the run"
    xlabel, ylabel = "seconds into the run", "Rolling window FPS"
    view.label_axes(ax, x=xlabel, y=ylabel)
    headroom(ax, y, 1.12)
    ax.set_xlim(float(x[0]), float(x[-1]) * 1.06)
    ax.set_title(view.titled(title))
    tidy(ax)
    if ax.get_legend_handles_labels()[0]:
        ax.legend(loc="lower right", ncol=2)

    return canvas.save(fig, applied(Chart(
        id="system_window_fps", file="", kind="trend", title=title,
        subtitle=(f"{len(data.cuts)} split-point change(s) marked"
                  if data.cuts and shown.has("cuts") else ""),
        summary=f"{len(y)} readings over {x[-1]:.0f}s; mean {mean:.2f} FPS, "
                f"peak {y.max():.2f}, settles at {last:.2f}",
        metrics=["window_fps"]),
        view, title=title, xlabel=xlabel, ylabel=ylabel, shown=shown), index=2)


# ------------------------------------------------ 03 · per-cluster timeline
def cluster_fps_timeline(canvas: Canvas, data: RunData, view: View) -> Chart | None:
    """C2/C3 — each cluster's own window FPS. Overlaid at ≤3, faceted past it.

    The rolling mean is what is drawn: two raw 300-point series on one axis is
    a scribble, and the spread they hide is the next chart's job (C4).
    """
    available = {c: p for c, p in sorted(data.cluster_fps.items()) if len(p) >= 8}
    if not available:
        return None
    colors = _cluster_colors(data)
    shown = Shown(view, [(c, c, colors.get(c, S1)) for c in available])
    series = [(r.key, available[r.key]) for r in shown]
    smoothing = max(stable_smooth(len(p), data.full_counts.get(c, 0))
                    for c, p in series)

    def draw(ax: plt.Axes, cluster: str, points: Sequence[Point], color: str) -> float:
        px, py = _xy(points)
        win = stable_smooth(len(py), data.full_counts.get(cluster, 0))
        line = rolling_mean(py, win) if win else py
        marker = dict(marker="o", **MARK_KW) if len(py) <= MARKER_MAX else {}
        ax.plot(px, line, color=color, label=cluster, **LINE_KW, **marker)
        return float(line[-1])

    title = "Throughput per cluster over the run"
    xlabel, ylabel = "seconds into the run", "Rolling window FPS"

    if len(series) <= 3:
        fig, ax = plt.subplots(figsize=(11.5, 4.3))
        for ref in shown:
            points = available[ref.key]
            last = draw(ax, ref.key, points, ref.color)
            _endpoint(ax, float(_xy(points)[0][-1]), last, ref.color, f"{last:.1f}")
        view.label_axes(ax, x=xlabel, y=ylabel)
        headroom(ax, [v for _, p in series for v in _xy(p)[1]], 1.12)
        # Start at the first reading, not at zero: no cluster has a window FPS
        # until it has 16 DONEs, and the empty run-up is not part of the story.
        left = min(float(_xy(p)[0][0]) for _, p in series)
        right = max(float(_xy(p)[0][-1]) for _, p in series)
        ax.set_xlim(left, right + (right - left) * 0.06)
        ax.set_title(view.titled(title))
        tidy(ax)
        if len(series) > 1:
            ax.legend(loc="lower right", ncol=len(series))
    else:
        # Small multiples: one series per panel, so no color encodes identity
        # and the three-slot all-pairs cap cannot be breached (§2).
        cols = min(3, len(series))
        rows = math.ceil(len(series) / cols)
        fig, axes = plt.subplots(rows, cols, figsize=(4.2 * cols, 2.9 * rows),
                                 sharey=True, squeeze=False)
        for ax, (cluster, points) in zip(axes.flat, series):
            draw(ax, cluster, points, S1)
            ax.set_title(cluster, fontsize=10.5, color=INK_2, pad=6)
            tidy(ax)
        for ax in list(axes.flat)[len(series):]:
            ax.set_visible(False)
        fig.supylabel(view.ylabel or ylabel, fontsize=10.5, color=INK_2)
        fig.supxlabel(view.xlabel or xlabel, fontsize=10.5, color=INK_2)
        suptitle(fig, view.titled(title))

    means = {c: float(np.mean([p.value for p in pts])) for c, pts in series}
    return canvas.save(fig, applied(Chart(
        id="cluster_window_fps", file="", kind="trend", title=title,
        subtitle=f"{smoothing}-reading rolling mean — the spread is chart 04"
                 if smoothing else "",
        summary=", ".join(f"{c} mean {v:.2f} FPS" for c, v in means.items()),
        metrics=["window_fps"]),
        view, title=title, xlabel=xlabel, ylabel=ylabel, shown=shown), index=3)


# ------------------------------------------------------- 04 · distribution
def fps_distribution(canvas: Canvas, data: RunData, view: View) -> Chart | None:
    """C4 — box per cluster. Spread and skew, which the smoothed line hides."""
    available = {c: np.array([p.value for p in pts], dtype=float)
                 for c, pts in sorted(data.cluster_fps.items()) if len(pts) >= 12}
    if not available:
        return None
    colors = _cluster_colors(data)
    shown = Shown(view, [(c, c, colors.get(c, S1)) for c in available])
    groups = [(r.key, available[r.key]) for r in shown]

    fig, ax = plt.subplots(figsize=(max(6.4, 1.8 * len(groups) + 3.0), 4.2))
    box = ax.boxplot(
        [v for _, v in groups], widths=0.44, patch_artist=True, showfliers=False,
        tick_labels=[c for c, _ in groups],
        medianprops=dict(color=SURFACE, linewidth=1.8),   # reads on the fill
        whiskerprops=dict(color=AXIS, linewidth=1.0),
        capprops=dict(color=AXIS, linewidth=1.0),
    )
    for patch, ref in zip(box["boxes"], shown):
        patch.set_facecolor(ref.color)
        patch.set_edgecolor(SURFACE)
        patch.set_linewidth(1.2)

    # Label the mean ABOVE THE WHISKER CAP — p75 sits inside the whisker (§C4).
    caps = []
    for i, (_, values) in enumerate(groups, start=1):
        q1, q3 = np.percentile(values, [25, 75])
        reach = 1.5 * (q3 - q1)
        top = float(values[values <= q3 + reach].max())
        caps += [top, float(values[values >= q1 - reach].min())]
        ax.annotate(f"mean {values.mean():.1f}", xy=(i, top), xytext=(0, 8),
                    textcoords="offset points", ha="center", fontsize=9, color=INK_2)

    # A box encodes position, not length, so the axis follows the data. Forcing
    # it to zero squeezes every IQR here into a sliver of the panel.
    band(ax, caps, pad=0.10, top_pad=0.22)
    title = "Window FPS distribution  (box = IQR, whiskers = 1.5×IQR)"
    xlabel, ylabel = "Cluster", "Rolling window FPS"
    view.label_axes(ax, x=xlabel, y=ylabel)
    # Say what the box *is* — readers do not agree on box conventions (§C4).
    ax.set_title(view.titled(title))
    tidy(ax, categorical="x")

    return canvas.save(fig, applied(Chart(
        id="window_fps_distribution", file="", kind="distribution",
        title="Window FPS distribution by cluster",
        subtitle="outliers suppressed — the series are dense",
        summary="; ".join(
            f"{c} median {np.median(v):.2f}, IQR {np.percentile(v, 25):.2f}–"
            f"{np.percentile(v, 75):.2f} FPS" for c, v in groups),
        metrics=["window_fps"]),
        view, title=title, xlabel=xlabel, ylabel=ylabel, shown=shown), index=4)


# ------------------------------------------------------ 05 · service latency
def service_latency(canvas: Canvas, data: RunData, view: View) -> Chart | None:
    """C5 — one panel per role, deliberately **not** `sharey`.

    Cloud service time runs an order of magnitude above the edge, so a shared
    axis would flatten the edge panel to nothing. Two honest scales with the
    role named on each is the guide's replacement for a dual axis (§1, C5).
    """
    rows = {(scope, role): stats for (scope, role, kind), stats in data.latency.items()
            if kind == "service"}
    roles = [r for r in data.roles if any(role == r for _, role in rows)]
    scopes = [s for s in data.clusters if any(scope == s for scope, _ in rows)]
    if not roles or not scopes:
        return None

    shown = Shown(view, [("mean_ms", "Mean", S1), ("p95_ms", "p95", S2)])
    x = np.arange(len(scopes), dtype=float)
    off = offsets(len(shown), WIDTH)
    fig, axes = plt.subplots(1, len(roles), figsize=(5.6 * len(roles), 4.2),
                             squeeze=False)   # NOT sharey — that is the point

    for ax, role in zip(axes[0], roles):
        heights: list[float] = []
        for i, ref in enumerate(shown):
            values = [rows.get((s, role), {}).get(ref.key, math.nan) for s in scopes]
            heights.extend(values)
            bars = ax.bar(x + off[i], values, WIDTH, label=ref.label,
                          color=ref.color, **BAR_KW)
            label_bars(ax, bars, values, "{:,.0f}")
        ax.set_xticks(x, scopes)
        ax.set_title(f"{ROLE_LABEL.get(role, role.capitalize())} devices")
        headroom(ax, heights, 1.20)
        tidy(ax, categorical="x")

    title, ylabel = "Service latency by device role  (lower is better)", "Service latency (ms)"
    view.label_axes(axes[0][0], y=ylabel)
    suptitle(fig, view.titled(title), y=1.13 if len(shown) > 1 else 1.02)
    if len(shown) > 1:
        panel_legend(fig, axes[0][0], ncol=len(shown))

    worst = max(rows.items(), key=lambda kv: kv[1].get("mean_ms", 0.0))
    return canvas.save(fig, applied(Chart(
        id="service_latency_by_role", file="", kind="comparison",
        title="Service latency by device role",
        subtitle="each panel keeps its own scale — the roles differ by an "
                 "order of magnitude",
        summary=f"slowest: {worst[0][1]} in {worst[0][0]} at "
                f"{fmt(worst[1].get('mean_ms'))} ms mean",
        metrics=["mean_ms", "p95_ms"]),
        view, title=title, ylabel=ylabel, shown=shown), index=5)


# ---------------------------------------------------------- 06 · e2e profile
def e2e_profile(canvas: Canvas, data: RunData, view: View) -> Chart | None:
    """C6 — mean/p50/p95/max per scope, `sharey`: same measure, must compare."""
    rows = {scope: stats for (scope, _, kind), stats in data.latency.items()
            if kind == "e2e"}
    scopes = [s for s in data.scopes if s in rows]
    if not scopes:
        return None

    shown = Shown(view, [("mean_ms", "Mean", S1), ("p50_ms", "p50", S1),
                         ("p95_ms", "p95", S1), ("max_ms", "Max", S1)])
    every = [rows[s].get(r.key, math.nan) for s in scopes for r in shown]
    # Convert once, here, rather than inside the label formatter (§C5).
    to_seconds = max((v for v in every if math.isfinite(v)), default=0.0) >= 2000
    scale, unit = (1000.0, "s") if to_seconds else (1.0, "ms")
    x = np.arange(len(shown), dtype=float)

    fig, axes = plt.subplots(1, len(scopes), figsize=(4.5 * len(scopes), 4.2),
                             sharey=True, squeeze=False)
    for ax, scope in zip(axes[0], scopes):
        values = [rows[scope].get(r.key, math.nan) / scale for r in shown]
        bars = ax.bar(x, values, min(0.56, 2.2 / max(len(shown), 1)), color=S1, **BAR_KW)
        label_bars(ax, bars, values, "{:,.1f}" if to_seconds else "{:,.0f}")
        ax.set_xticks(x, shown.labels)
        ax.set_title(scope)
        tidy(ax, categorical="x")
    headroom(axes[0][0], [v / scale for v in every], 1.16)

    title = "End-to-end latency profile  (lower is better)"
    ylabel = f"End-to-end latency ({unit})"
    view.label_axes(axes[0][0], y=ylabel)
    suptitle(fig, view.titled(title))

    system = rows.get(SYSTEM) or rows[scopes[-1]]
    return canvas.save(fig, applied(Chart(
        id="e2e_latency_profile", file="", kind="comparison",
        title="End-to-end latency profile",
        subtitle="one scale across the panels — the same measure, so they must "
                 "be comparable",
        summary=f"system mean {system.get('mean_ms', math.nan) / scale:,.1f} {unit}, "
                f"p95 {system.get('p95_ms', math.nan) / scale:,.1f} {unit} "
                f"over {fmt(system.get('n'))} samples",
        metrics=["mean_ms", "p50_ms", "p95_ms", "max_ms"]),
        view, title=title, ylabel=ylabel, shown=shown), index=6)


# ----------------------------------------------------------- 07 · utilization
def utilization_by_role(canvas: Canvas, data: RunData, view: View) -> Chart | None:
    """C7 — cluster × role, plus the system total. Colored by role, not by rank.

    Cloud pinned high beside an idle edge is the read the guide's §7 recipe
    asks this file for: the cut is in the wrong place.
    """
    present: list[str] = []
    for role in [*data.roles, "all"]:
        if any(r == role and math.isfinite(stats.get("utilization", math.nan))
               for (_, r), stats in data.utilization.items()):
            present.append(role)
    shown = Shown(view, [(r, ROLE_LABEL.get(r, r), ROLE_COLOR.get(r, MUTED))
                         for r in present])
    drawn = {r.key for r in shown}

    rows: list[tuple[str, str, float]] = []
    for cluster in data.clusters:
        for role in data.roles:
            stats = data.utilization.get((cluster, role))
            if role in drawn and stats and math.isfinite(stats.get("utilization", math.nan)):
                rows.append((cluster, role, stats["utilization"]))
    system = data.utilization.get((SYSTEM, "all"))
    if "all" in drawn and system and math.isfinite(system.get("utilization", math.nan)):
        rows.append((SYSTEM, "all", system["utilization"]))
    if len(rows) < 2:
        return None

    # Two-line tick labels beat rotation — rotated labels are slow to read (§C7).
    labels = [f"{'C' + c.split()[-1] if c != SYSTEM else 'System'}\n{r}"
              for c, r, _ in rows]
    colors = [ROLE_COLOR.get(r, MUTED) for _, r, _ in rows]

    fig, ax = plt.subplots(figsize=(max(7.0, 1.25 * len(rows) + 3.0), 4.2))
    x = np.arange(len(rows), dtype=float)
    bars = ax.bar(x, [v for *_, v in rows], 0.62, color=colors, **BAR_KW)
    label_bars(ax, bars, [v for *_, v in rows], "{:.1f}%", fontsize=9)
    ax.set_xticks(x, labels, fontsize=9.5)
    title, ylabel = "Device utilization by cluster and role", "Utilization (%)"
    view.label_axes(ax, y=ylabel)
    ax.set_ylim(0, 118)          # percentages: fix the ceiling, never autoscale
    ax.set_title(view.titled(title))
    tidy(ax, categorical="x")

    used = list(dict.fromkeys(r for _, r, _ in rows))
    ax.legend(
        [plt.Rectangle((0, 0), 1, 1, color=ROLE_COLOR.get(r, MUTED)) for r in used],
        [ROLE_LABEL.get(r, r) for r in used], loc="upper right", ncol=len(used),
    )

    by_role: dict[str, list[float]] = {}
    for _, role, value in rows:
        by_role.setdefault(role, []).append(value)
    if "cloud" in by_role and "edge" in by_role:
        cloud, edge = np.mean(by_role["cloud"]), np.mean(by_role["edge"])
        gap = cloud - edge
        takeaway(ax, f"cloud averages {cloud:.0f}% against {edge:.0f}% on the edge — "
                     + ("the cut is too shallow" if gap > 25 else
                        "the cut is too deep" if gap < -25 else "the split is balanced"))

    return canvas.save(fig, applied(Chart(
        id="utilization_by_role", file="", kind="comparison",
        title=title,
        subtitle="pooled Σbusy/Σtotal, so a long-running device counts for more",
        summary="; ".join(f"{c} {r} {v:.1f}%" for c, r, v in rows),
        metrics=["utilization"]),
        view, title=title, ylabel=ylabel, shown=shown), index=7)


# ------------------------------------------------------- 08 · per-device bars
def device_utilization(canvas: Canvas, data: RunData, view: View) -> Chart | None:
    """C8 — every device individually, blocked by role and ranked inside it."""
    every = [d for d in data.devices
             if math.isfinite(float(d.get("utilization", math.nan)))]
    if len(every) < 2:
        return None

    present = [r for r in data.roles if any(str(d["role"]) == r for d in every)]
    shown = Shown(view, [(r, ROLE_LABEL.get(r, r), ROLE_COLOR.get(r, MUTED))
                         for r in present])
    drawn = {r.key for r in shown}
    devices = [d for d in every if str(d["role"]) in drawn]
    if len(devices) < 2:
        # Hiding a role down to a single device leaves nothing to rank; show
        # them all rather than dropping the card without explanation.
        devices = every

    order = {role: i for i, role in enumerate(data.roles)}
    # Sort by [class, value] so classes stay blocked *and* rank inside a class
    # is visible (§C8).
    devices.sort(key=lambda d: (order.get(str(d["role"]), 9), -float(d["utilization"])))
    values = [float(d["utilization"]) for d in devices]
    colors = [ROLE_COLOR.get(str(d["role"]), MUTED) for d in devices]

    ticks = _role_ticks(devices)

    fig, ax = plt.subplots(figsize=(max(7.0, 0.62 * len(devices) + 2.6), 4.2))
    x = np.arange(len(devices), dtype=float)
    bars = ax.bar(x, values, 0.72, color=colors, **BAR_KW)
    label_bars(ax, bars, values, "{:.0f}", fontsize=8, color=MUTED)
    ax.set_xticks(x, ticks, fontsize=8.5)
    title = (f"Per-device utilization  —  {np.mean(values):.1f}% mean "
             f"across {len(devices)} devices")
    xlabel = "Device  (" + ", ".join(
        f"{r.key[:1].upper()} = {r.label}" for r in shown) + ")"
    ylabel = "Utilization (%)"
    view.label_axes(ax, x=xlabel, y=ylabel)
    ax.set_ylim(0, 118)
    ax.set_title(view.titled(title))
    tidy(ax, categorical="x")
    used = list(dict.fromkeys(str(d["role"]) for d in devices))
    ax.legend(
        [plt.Rectangle((0, 0), 1, 1, color=ROLE_COLOR.get(r, MUTED)) for r in used],
        [ROLE_LABEL.get(r, r) for r in used], loc="upper right", ncol=len(used),
    )

    idle = min(devices, key=lambda d: float(d["utilization"]))
    return canvas.save(fig, applied(Chart(
        id="device_utilization", file="", kind="comparison",
        title="Per-device utilization",
        subtitle="ranked inside each role — a lone short bar is a straggler",
        summary=f"{len(devices)} devices, {min(values):.1f}%–{max(values):.1f}%; "
                f"quietest is {idle['role']} at {float(idle['utilization']):.1f}%",
        metrics=["utilization"]),
        view, title=title, xlabel=xlabel, ylabel=ylabel, shown=shown), index=8)


# ---------------------------------------------------------- 09 · mAP by window
def accuracy_by_window(canvas: Canvas, data: RunData, view: View) -> Chart | None:
    """C9 — two measures of the same phenomenon, so two panels and no `sharey`.

    §III.4 first: if the clusters scored identically, one line is drawn and the
    caption says so. Two perfectly overlapping lines imply a comparison that is
    not in the data — the reader cannot tell whether the other is hidden or
    missing.
    """
    available = {c: rows for c, rows in sorted(data.accuracy_window.items()) if rows}
    if not available:
        return None

    def column(rows: list[dict[str, float]], key: str) -> np.ndarray:
        return np.array([r.get(key, math.nan) for r in rows], dtype=float)

    note = ""
    if len(available) > 1:
        first = next(iter(available))
        identical = all(
            len(rows) == len(available[first])
            and np.allclose(column(rows, "mAP50"), column(available[first], "mAP50"),
                            equal_nan=True)
            and np.allclose(column(rows, "mAP50_95"),
                            column(available[first], "mAP50_95"), equal_nan=True)
            for rows in available.values()
        )
        if identical:
            note = f"identical across all {len(available)} clusters — one line drawn"
            available = {first: available[first]}

    colors = _cluster_colors(data)
    shown = Shown(view, [(c, c, colors.get(c, S1)) for c in available])
    panels = [("mAP50", "mAP@50"), ("mAP50_95", "mAP@50:95")]
    title = "Detection accuracy by sliding window  (higher is better)"
    xlabel = "Window index"

    fig, axes = plt.subplots(1, 2, figsize=(12.4, 4.2))
    for ax, (key, panel_title) in zip(axes, panels):
        for ref in shown:
            rows = available[ref.key]
            px, py = column(rows, "window"), column(rows, key)
            marker = dict(marker="o", **MARK_KW) if len(py) <= MARKER_MAX else {}
            ax.plot(px, py, color=ref.color, label=ref.key, **LINE_KW, **marker)
            _endpoint(ax, float(px[-1]), float(py[-1]), ref.color, f"{py[-1]:.3f}")
        ax.set_title(panel_title)
        view.label_axes(ax, x=xlabel, y=panel_title)
        ax.margins(x=0.10)
        tidy(ax)
    suptitle(fig, view.titled(title), y=1.13 if len(shown) > 1 else 1.02)
    if len(shown) > 1:
        panel_legend(fig, axes[0], ncol=len(shown))

    return canvas.save(fig, applied(Chart(
        id="map_by_window", file="", kind="trend",
        title="Detection accuracy by sliding window",
        subtitle=note or f"{len(next(iter(available.values())))} windows of "
                         f"{fmt(next(iter(available.values()))[0].get('frames'))} frames",
        summary="; ".join(
            f"{c} mAP@50 {np.nanmean(column(r, 'mAP50')):.4f} mean, "
            f"{np.nanmin(column(r, 'mAP50')):.4f}–{np.nanmax(column(r, 'mAP50')):.4f}"
            for c, r in available.items()),
        metrics=["mAP50", "mAP50_95"]),
        view, title=title, xlabel=xlabel, ylabel="mAP@50", shown=shown), index=9)


# ----------------------------------------------------------- 10 · mAP summary
def accuracy_summary(canvas: Canvas, data: RunData, view: View) -> Chart | None:
    """C10 — WINDOW against ALL, per scope. Slot 3 is in play, so every bar is labelled."""
    scopes = [s for s in [*data.clusters, OVERALL]
              if any(scope == s for scope, _ in data.accuracy)]
    aggs = [a for a in ("WINDOW", "ALL") if any(agg == a for _, agg in data.accuracy)]
    if not scopes or not aggs:
        return None
    if len(scopes) > 3:
        # Past three series every pair can end up side by side; fold the tail.
        scopes = scopes[:2] + [OVERALL] if OVERALL in scopes else scopes[:3]

    colors = entity_colors(scopes)
    shown = Shown(view, [(s, s, colors[s]) for s in scopes])
    width = 0.26 if len(shown) > 2 else WIDTH
    x = np.arange(len(aggs), dtype=float)
    off = offsets(len(shown), width, 0.02)
    labels = {"WINDOW": "Window mean", "ALL": "All frames"}

    panels = [("mAP50", "mAP@50"), ("mAP50_95", "mAP@50:95")]
    title = "Detection accuracy summary  (higher is better)"
    fig, axes = plt.subplots(1, 2, figsize=(11.6, 4.2))
    for ax, (key, panel_title) in zip(axes, panels):
        heights: list[float] = []
        for i, ref in enumerate(shown):
            values = [data.accuracy.get((ref.key, agg), {}).get(key, math.nan)
                      for agg in aggs]
            heights.extend(values)
            bars = ax.bar(x + off[i], values, width, label=ref.label,
                          color=ref.color, **BAR_KW)
            label_bars(ax, bars, values, "{:.4f}", fontsize=8)  # relief: mandatory
        ax.set_xticks(x, [labels.get(a, a) for a in aggs])
        ax.set_title(panel_title)
        view.label_axes(ax, y=panel_title)
        headroom(ax, heights, 1.16)
        tidy(ax, categorical="x")
    suptitle(fig, view.titled(title), y=1.13 if len(shown) > 1 else 1.02)
    if len(shown) > 1:
        panel_legend(fig, axes[0], ncol=len(shown))

    head = data.accuracy.get((OVERALL, "ALL")) or data.accuracy.get((scopes[0], "ALL"))
    return canvas.save(fig, applied(Chart(
        id="map_summary", file="", kind="comparison",
        title="Detection accuracy summary",
        subtitle="ALL weights every scorable frame once — the headline number; "
                 "WINDOW over-weights frames that sit in several windows",
        summary=(f"headline mAP@50 {head.get('mAP50', math.nan):.4f}, "
                 f"mAP@50:95 {head.get('mAP50_95', math.nan):.4f}") if head else "",
        metrics=["mAP50", "mAP50_95"]),
        view, title=title, ylabel="mAP@50", shown=shown), index=10)


# --------------------------------------------------------- 11 · queue wait
def queue_wait(canvas: Canvas, data: RunData, view: View) -> Chart | None:
    """Service beside pipeline. The gap between them is buffering, not devices.

    `service` is the device's own `get input -> output`; `pipeline` is the same
    unit from ready to published, so it additionally carries the in-process
    hand-off queue waits. It therefore scales with queue depth rather than with
    device speed -- a run at depth 4 measured 11.6 s service against 57.6 s
    pipeline on the same hardware. When the second bar towers over the first,
    the fix is a shorter queue, and throughput does not depend on it (§2.6).
    """
    pairs: dict[tuple[str, str], dict[str, dict[str, float]]] = {}
    for (scope, role, kind), stats in data.latency.items():
        if kind in ("service", "pipeline"):
            pairs.setdefault((scope, role), {})[kind] = stats
    # Only worth a chart when the run wrote the second measure: one bar per
    # cluster repeated from chart 05 says nothing chart 05 did not.
    if not any("pipeline" in kinds for kinds in pairs.values()):
        return None

    roles = [r for r in data.roles if any(role == r for _, role in pairs)]
    scopes = [s for s in data.clusters if any(scope == s for scope, _ in pairs)]
    if not roles or not scopes:
        return None

    shown = Shown(view, [("service", "Service", S1), ("pipeline", "Pipeline", S2)])
    every = [pairs.get((s, r), {}).get(k.key, {}).get("mean_ms", math.nan)
             for s in scopes for r in roles for k in shown]
    to_seconds = max((v for v in every if math.isfinite(v)), default=0.0) >= 2000
    scale, unit = (1000.0, "s") if to_seconds else (1.0, "ms")

    x = np.arange(len(scopes), dtype=float)
    off = offsets(len(shown), WIDTH)
    # Cloud service time runs an order of magnitude above the edge, so this is
    # the same two-honest-scales panel split chart 05 uses, for the same reason.
    fig, axes = plt.subplots(1, len(roles), figsize=(5.6 * len(roles), 4.3),
                             squeeze=False)
    for ax, role in zip(axes[0], roles):
        heights: list[float] = []
        for i, ref in enumerate(shown):
            values = [pairs.get((s, role), {}).get(ref.key, {}).get("mean_ms", math.nan)
                      / scale for s in scopes]
            heights.extend(values)
            bars = ax.bar(x + off[i], values, WIDTH, label=ref.label,
                          color=ref.color, **BAR_KW)
            label_bars(ax, bars, values, "{:,.1f}" if to_seconds else "{:,.0f}")
        ax.set_xticks(x, scopes)
        ax.set_title(f"{ROLE_LABEL.get(role, role.capitalize())} devices")
        headroom(ax, heights, 1.20)
        tidy(ax, categorical="x")

    title = "Queue wait: pipeline against service latency  (lower is better)"
    ylabel = f"Mean latency ({unit})"
    view.label_axes(axes[0][0], y=ylabel)
    suptitle(fig, view.titled(title), y=1.13 if len(shown) > 1 else 1.02)
    if len(shown) > 1:
        panel_legend(fig, axes[0][0], ncol=len(shown))

    ratios = {
        key: kinds["pipeline"].get("mean_ms", math.nan) / kinds["service"]["mean_ms"]
        for key, kinds in pairs.items()
        if "pipeline" in kinds and kinds.get("service", {}).get("mean_ms", 0.0) > 0
    }
    live = {k: v for k, v in ratios.items() if math.isfinite(v)}
    if live:
        worst = max(live, key=live.get)
        note = (f"{worst[1]} in {worst[0]} waits {live[worst]:.1f}× its own service "
                f"time" + (" — shorten the queue" if live[worst] >= 2 else
                           " — little buffering"))
        takeaway(axes[0][len(roles) // 2], note, y=-0.20)

    return canvas.save(fig, applied(Chart(
        id="queue_wait", file="", kind="comparison",
        title="Pipeline against service latency",
        subtitle="the difference is time spent in the hand-off queue, not on "
                 "the device — each panel keeps its own scale",
        summary="; ".join(f"{scope} {role} ×{ratio:.1f}"
                          for (scope, role), ratio in live.items()),
        metrics=["mean_ms"]),
        view, title=title, ylabel=ylabel, shown=shown), index=11)


# ---------------------------------------------------- 12 · free time, device
def device_free_time(canvas: Canvas, data: RunData, view: View) -> Chart | None:
    """Free time beside utilization, per device. The two do not sum to 100%.

    They are not the same measurement seen twice. Utilization is `busy/total`
    over one lane's `get input -> output` window; free time is the whole run
    minus the **union** of every lane's busy intervals. A wait inside the unit
    window counts as busy for one and free for the other, and work on a second
    lane counts for the other and not at all for the first. So a device at 40%
    utilization and 3% free is not idle -- it is doing work the unit window
    never saw, and that gap is the finding (§2.10).
    """
    every = [d for d in data.free_devices
             if math.isfinite(float(d.get("free", math.nan)))]
    if len(every) < 2:
        return None

    busy = {str(d["client"]): float(d["utilization"]) for d in data.devices
            if d.get("client") and math.isfinite(float(d.get("utilization", math.nan)))}
    matched = any(str(d["client"]) in busy for d in every)

    shown = Shown(view, [
        ("free", "Free (all lanes idle)", S1),
        *([("utilization", "Busy (unit window)", S2)] if matched else []),
    ])
    order = {role: i for i, role in enumerate(data.roles)}
    devices = sorted(every, key=lambda d: (order.get(str(d["role"]), 9), -float(d["free"])))
    ticks = _role_ticks(devices)

    x = np.arange(len(devices), dtype=float)
    width = 0.38 if len(shown) > 1 else 0.68
    off = offsets(len(shown), width)
    fig, ax = plt.subplots(figsize=(max(7.0, 0.78 * len(devices) + 2.8), 4.3))
    for i, ref in enumerate(shown):
        values = [float(d["free"]) if ref.key == "free"
                  else busy.get(str(d["client"]), math.nan) for d in devices]
        bars = ax.bar(x + off[i], values, width, label=ref.label,
                      color=ref.color, **BAR_KW)
        label_bars(ax, bars, values, "{:.0f}", fontsize=8, color=MUTED)

    ax.set_xticks(x, ticks, fontsize=8.5)
    title = "Free time and utilization, per device"
    xlabel = "Device  (" + ", ".join(
        f"{r[:1].upper()} = {ROLE_LABEL.get(r, r)}" for r in data.roles
        if any(str(d["role"]) == r for d in devices)) + ")"
    ylabel = "Share of the device's run (%)"
    view.label_axes(ax, x=xlabel, y=ylabel)
    ax.set_ylim(0, 118)          # percentages: fix the ceiling, never autoscale
    ax.set_title(view.titled(title))
    tidy(ax, categorical="x")
    if len(shown) > 1:
        ax.legend(loc="upper right", ncol=len(shown))

    values = [float(d["free"]) for d in devices]
    idlest = max(devices, key=lambda d: float(d["free"]))
    gaps = [d for d in devices
            if math.isfinite(busy.get(str(d["client"]), math.nan))
            and float(d["free"]) + busy[str(d["client"])] < 85.0]
    if gaps:
        worst = min(gaps, key=lambda d: float(d["free"]) + busy[str(d["client"])])
        takeaway(ax, f"{worst['role']} {ticks[devices.index(worst)]} is "
                     f"{float(worst['free']):.0f}% free at "
                     f"{busy[str(worst['client'])]:.0f}% utilization — the rest is "
                     f"work the unit window never saw")
    else:
        takeaway(ax, f"{np.mean(values):.0f}% of the fleet's wall clock was idle; "
                     f"quietest is {idlest['role']} at {float(idlest['free']):.0f}% free")

    return canvas.save(fig, applied(Chart(
        id="device_free_time", file="", kind="comparison",
        title="Free time and utilization, per device",
        subtitle="free% + utilization% ≠ 100% by construction — different "
                 "scopes, both measured, and the gap is the finding",
        summary=f"{len(devices)} devices, {min(values):.1f}%–{max(values):.1f}% free; "
                f"idlest is {idlest['role']} at {float(idlest['free']):.1f}%",
        metrics=["free", "utilization"]),
        view, title=title, xlabel=xlabel, ylabel=ylabel, shown=shown), index=12)


def _breakdown(rows: dict[tuple[str, str], dict[str, float]], scope: str,
               key: str, *, renormalise: bool) -> list[tuple[str, float, float]]:
    """`(name, seconds, share)` for one scope, largest first.

    With no `SYSTEM` line the per-cluster rows are summed, which is sound:
    seconds are additive across scopes. Their *shares* are not always -- free
    reasons are exclusive and re-divide cleanly, busy kinds overlap across
    lanes and do not -- so `renormalise` says which, and a share that cannot
    honestly be recomputed comes back as `nan` and simply is not printed.
    """
    seconds: dict[str, float] = {}
    stated: dict[str, float] = {}
    for (row_scope, name), stats in rows.items():
        if scope and row_scope != scope:
            continue
        value = stats.get(key, math.nan)
        if not math.isfinite(value):
            continue
        seconds[name] = seconds.get(name, 0.0) + value
        stated[name] = stats.get("share", math.nan) if scope else math.nan

    total = sum(seconds.values())
    out = []
    for name, value in seconds.items():
        share = stated[name]
        if not math.isfinite(share) and renormalise and total:
            share = value / total * 100.0
        out.append((name, value, share))
    return sorted(out, key=lambda row: -row[1])


# ------------------------------------------------- 13 · free time, breakdown
def free_time_breakdown(canvas: Canvas, data: RunData, view: View) -> Chart | None:
    """Why the fleet was free, and where its busy time went.

    Two part-to-whole readings that obey different arithmetic, which is why
    they are two panels and why the subtitle says so. `FREE reason=` shares are
    attributed in a fixed priority and sum to exactly 100% of the scope's free
    time, with whatever no reason covers carried as `unaccounted`. `KIND kind=`
    shares **overlap** -- a pipelined device runs its lanes at once -- so they
    may sum past 100%, and only the merged busy total is exclusive (§2.11).

    Neither panel colors by rank: every bar in a panel wears one hue and the
    name is on the tick, so re-ordering the run cannot repaint the chart.
    """
    scope = data.free_scope()
    reasons = _breakdown(data.free_reasons, scope, "free_s", renormalise=True)
    kinds = _breakdown(data.free_kinds, scope, "busy_s", renormalise=False)
    if not reasons and not kinds:
        return None

    panels = [
        ("reason", "Why it was free", S1, reasons, "Free time (s)"),
        ("kind", "Where the busy time went", S2, kinds, "Busy time (s)"),
    ]
    shown = Shown(view, [(key, label, color) for key, label, color, rows, _ in panels
                         if rows])
    drawn = [p for p in panels if shown.has(p[0]) and p[3]]
    if not drawn:
        return None

    tallest = max(len(rows) for _, _, _, rows, _ in drawn)
    fig, axes = plt.subplots(1, len(drawn), squeeze=False,
                             figsize=(6.2 * len(drawn), max(3.4, 0.42 * tallest + 2.2)))
    for ax, (_, label, color, rows, axis_label) in zip(axes[0], drawn):
        y = np.arange(len(rows), dtype=float)[::-1]
        bars = ax.barh(y, [seconds for _, seconds, _ in rows], 0.62,
                       color=color, **BAR_KW)
        ax.bar_label(
            bars, padding=4, fontsize=8.5, color=INK_2,
            labels=[f"{seconds:,.0f}s" + (f"  {share:.0f}%" if math.isfinite(share) else "")
                    for _, seconds, share in rows],
        )
        ax.set_yticks(y, [name for name, _, _ in rows], fontsize=9.5)
        ax.set_title(label)
        ax.set_xlabel(axis_label)
        ax.set_xlim(0, max(seconds for _, seconds, _ in rows) * 1.30)
        tidy(ax, categorical="y")

    title = "Free time by reason, busy time by kind"
    where = scope or "all clusters"
    suptitle(fig, view.titled(title))

    top = reasons[0] if reasons else None
    return canvas.save(fig, applied(Chart(
        id="free_time_breakdown", file="", kind="distribution",
        title=title,
        subtitle=f"{where} · reasons are exclusive and sum to the scope's free "
                 "time; kinds overlap across lanes and may sum past its span",
        summary=(f"largest reason {top[0]} at {top[1]:,.0f}s"
                 + (f" ({top[2]:.0f}% of free time)" if math.isfinite(top[2]) else "")
                 if top else "; ".join(f"{n} {v:,.0f}s" for n, v, _ in kinds)),
        metrics=["free_s", "busy_s", "share"]),
        view, title=title, shown=shown), index=13)


# ------------------------------------------------- 14 · free time, machines
def machine_free_time(canvas: Canvas, data: RunData, view: View) -> Chart | None:
    """Per host: what the pipeline left idle, against what the OS saw idle.

    A machine is free only when **none** of the device processes on it is
    working, which cannot be recovered from their individual ratios -- two
    processes each 50% free can keep a host 100% busy by interleaving. So these
    come from the union of their busy intervals, safe here and nowhere else
    because processes on one host share a clock.

    `host_idle` is the OS's own accounting across every process on the box, and
    the two disagreeing is the informative case: idle pipeline on a busy host
    means something else is eating the CPU; busy pipeline on an idle host means
    it is blocked on I/O rather than on compute (§2.11).
    """
    def value(row: dict[str, object], key: str) -> float:
        return float(row.get(key, math.nan) or math.nan)

    # The host running only the controller carries `host_idle` and no pipeline
    # figure of its own. It is kept: a fleet view that leaves out the machine
    # holding the controller is not a fleet view, and `grouped_x` centres its
    # lone bar on the tick rather than parking it beside an empty slot.
    rows = [m for m in data.machines
            if math.isfinite(value(m, "free")) or math.isfinite(value(m, "host_idle"))]
    if len(rows) < 2:
        return None
    rows.sort(key=lambda m: (not math.isfinite(value(m, "free")),
                             -(value(m, "free") if math.isfinite(value(m, "free"))
                               else value(m, "host_idle"))))

    has_idle = any(math.isfinite(value(m, "host_idle")) for m in rows)
    shown = Shown(view, [
        ("free", "Pipeline free", S1),
        *([("host_idle", "Host idle (OS)", S2)] if has_idle else []),
    ])
    width = 0.38 if len(shown) > 1 else 0.62
    heights = [[value(m, ref.key) for m in rows] for ref in shown]
    positions = grouped_x(heights, width)

    fig, ax = plt.subplots(figsize=(max(7.0, 1.15 * len(rows) + 3.0), 4.3))
    x = np.arange(len(rows), dtype=float)
    for i, ref in enumerate(shown):
        bars = ax.bar(positions[i], heights[i], width, label=ref.label,
                      color=ref.color, **BAR_KW)
        label_bars(ax, bars, heights[i], "{:.0f}", fontsize=8.5)

    ax.set_xticks(x, [str(m["machine"]) for m in rows], fontsize=9)
    title = "Idle time by machine"
    xlabel, ylabel = "Machine", "Share of the run (%)"
    view.label_axes(ax, x=xlabel, y=ylabel)
    ax.set_ylim(0, 118)
    ax.set_title(view.titled(title))
    tidy(ax, categorical="x")
    if len(shown) > 1:
        ax.legend(loc="upper right", ncol=len(shown))

    paired = [m for m in rows if math.isfinite(value(m, "free"))
              and math.isfinite(value(m, "host_idle"))]
    if paired:
        quietest = paired[0]
        free, idle = value(quietest, "free"), value(quietest, "host_idle")
        # The four-way read of §2.11: the two numbers answer different
        # questions, and which way they disagree names the cause.
        reading = ("spare capacity — run fewer machines or give them more work"
                   if free > 50 and idle > 50 else
                   "something else on the box is using the CPU" if free > 50 else
                   "blocked on I/O or the network, not on compute" if idle > 50 else
                   "saturated")
        takeaway(ax, f"{quietest['machine']}: {free:.0f}% pipeline free, "
                     f"{idle:.0f}% host idle — {reading}")

    slop = sum(value(m, "merge_slop_s") for m in rows
               if math.isfinite(value(m, "merge_slop_s")))
    return canvas.save(fig, applied(Chart(
        id="machine_free_time", file="", kind="comparison",
        title=title,
        subtitle="from the union of every device process on the host, never "
                 "from their ratios"
                 + (f" · {slop:,.1f}s swallowed by the interval cap, so this "
                    "reads slightly low" if slop > 0 else ""),
        summary="; ".join(
            f"{m['machine']} " + (f"{value(m, 'free'):.1f}% free"
                                  if math.isfinite(value(m, "free"))
                                  else f"{value(m, 'host_idle'):.1f}% host idle, "
                                       "no pipeline stage")
            for m in rows),
        metrics=["free", "host_idle"]),
        view, title=title, xlabel=xlabel, ylabel=ylabel, shown=shown), index=14)


# --------------------------------------------------- 15 · free time, in time
def free_time_over_run(canvas: Canvas, data: RunData, view: View) -> Chart | None:
    """A heat map: one row per device, time across, free share as the color.

    Magnitude, so the color is one hue light to dark -- never a rainbow, and
    never a categorical slot, because the quantity being encoded is *how much*
    rather than *which*.

    The x axis is each device's **own** `t_offset_s`. Devices do not start
    together, so two cells in one column are not the same instant; the rows say
    when each device was idle relative to its own run, and lining them up in
    absolute time is exactly what this file does not support (§2.12).
    """
    clients = [c for c, points in data.free_series.items() if len(points) >= 2]
    if not clients:
        return None

    meta = data.free_series_meta
    order = {role: i for i, role in enumerate(data.roles)}
    present = [r for r in data.roles
               if any(meta.get(c, {}).get("role") == r for c in clients)]
    shown = Shown(view, [(r, ROLE_LABEL.get(r, r), ROLE_COLOR.get(r, MUTED))
                         for r in present])
    kept = {r.key for r in shown}
    clients = [c for c in clients if meta.get(c, {}).get("role", "") in kept] or clients
    clients.sort(key=lambda c: (order.get(meta.get(c, {}).get("role", ""), 9),
                                meta.get(c, {}).get("cluster", ""), c))

    width = max(len(data.free_series[c]) for c in clients)
    # Ragged rows padded with nan rather than zero: a device that had stopped
    # is not a device that was busy, and the colormap paints the gap as surface.
    grid = np.full((len(clients), width), np.nan)
    for row, client in enumerate(clients):
        for point in data.free_series[client]:
            grid[row, point.index] = point.value

    bucket = num(next((meta.get(c, {}).get("bucket_s", "") for c in clients), ""))
    span = width * bucket if math.isfinite(bucket) and bucket > 0 else float(width)
    cmap = LinearSegmentedColormap.from_list("free", pal.SEQUENTIAL)
    cmap.set_bad(SURFACE)

    fig, ax = plt.subplots(figsize=(11.5, max(3.0, 0.34 * len(clients) + 1.9)))
    mesh = ax.imshow(grid, aspect="auto", origin="upper", cmap=cmap,
                     vmin=0.0, vmax=100.0, interpolation="nearest",
                     extent=(0.0, span, len(clients) - 0.5, -0.5))
    ax.set_yticks(np.arange(len(clients), dtype=float), _role_ticks(
        [{"role": meta.get(c, {}).get("role", "unknown")} for c in clients]),
        fontsize=8.5)
    bar = fig.colorbar(mesh, ax=ax, pad=0.015, fraction=0.035)
    bar.set_label("Free (% of the bucket)", fontsize=10, color=INK_2)
    bar.outline.set_visible(False)

    title = "Free time over the run, per device"
    xlabel = ("seconds into that device's own run"
              if math.isfinite(bucket) and bucket > 0 else "bucket index")
    ylabel = "Device"
    view.label_axes(ax, x=xlabel, y=ylabel)
    ax.set_title(view.titled(title))
    ax.grid(visible=False)
    tidy(ax, hide=("top", "right", "left"))
    takeaway(ax, "read against chart 02: a band of free time that lines up with "
                 "a throughput dip names the stage that stalled", y=-0.24)

    means = {c: float(np.nanmean([p.value for p in data.free_series[c]]))
             for c in clients}
    return canvas.save(fig, applied(Chart(
        id="free_time_series", file="", kind="trend",
        title=title,
        subtitle=(f"{fmt(bucket)}s buckets · " if math.isfinite(bucket) else "")
                 + "each row on its own device's clock — a column is not one "
                   "instant across rows",
        summary="; ".join(f"{meta.get(c, {}).get('role', '?')} {value:.0f}% free "
                          f"on average" for c, value in means.items()),
        metrics=["free"]),
        view, title=title, xlabel=xlabel, ylabel=ylabel, shown=shown), index=15)


# ------------------------------------------------------ 16 · queue-host RAM
def broker_ram_timeline(canvas: Canvas, data: RunData, view: View) -> Chart | None:
    """The queue host's memory over the run, with throughput under it.

    Nothing of ours runs on that machine, and it is on the critical path
    anyway. A broker at its high-water mark does not fail -- it blocks
    publishers -- and on the worker that looks like a stall with no local
    cause. *The next stage is slow* and *the broker stopped accepting* produce
    almost identical worker-side telemetry, and this curve is what separates
    them (§2.13).

    Throughput goes in a second panel rather than on a second y axis. Two
    scales on one frame let the reader place the crossing wherever the drawing
    happened to put it; stacked panels on a shared x show the same coincidence
    and cannot be tuned into saying something else.
    """
    samples = [s for s in data.broker_samples
               if math.isfinite(s.get("used_mb", math.nan))]
    if len(samples) < 4:
        return None

    at = np.array([s["at"] for s in samples], dtype=float)
    columns = {
        "used": ("Used (MemTotal − MemAvailable)", S1, "used_mb"),
        "rss": ("Broker process RSS", S2, "rss_mb"),
        "swap": ("Swap used", S3, "swap_used_mb"),
    }
    refs = [(key, label, color) for key, (label, color, field_) in columns.items()
            if any(math.isfinite(s.get(field_, math.nan)) and s.get(field_, 0.0) > 0
                   for s in samples)]
    if data.system_fps:
        refs.append(("throughput", "System throughput", pal.SLOTS_LIGHT[3]))
    shown = Shown(view, refs)

    curves = [r for r in shown if r.key != "throughput"]
    with_fps = shown.has("throughput") and bool(data.system_fps)
    if not curves and not with_fps:
        return None

    heights = [2.6, 1.5] if (curves and with_fps) else [2.6]
    fig, axes = plt.subplots(len(heights), 1, figsize=(11.5, 1.55 * sum(heights)),
                             sharex=True, squeeze=False,
                             gridspec_kw=dict(height_ratios=heights))
    panels = list(axes[:, 0])
    top = panels[0] if curves else None

    total = next((s["total_mb"] for s in samples
                  if math.isfinite(s.get("total_mb", math.nan))), math.nan)
    peak = max((s["used_mb"] for s in samples), default=math.nan)
    if top is not None:
        for ref in curves:
            values = np.array([s.get(columns[ref.key][2], math.nan) for s in samples],
                              dtype=float)
            top.plot(at, values, color=ref.color, label=ref.label, **LINE_KW)
            _endpoint(top, float(at[-1]), float(values[-1]), ref.color,
                      f"{values[-1]:,.0f}")
        if math.isfinite(total) and peak >= total * 0.5:
            # The wall is only drawn when the run came close to it. Anchoring a
            # 1.8 GB curve to a 5.9 GB ceiling flattens it into a horizontal
            # line and costs the chart the one shape it exists to show; chart
            # 17 answers "how full did it get" on its own axis instead.
            top.axhline(total, color=AXIS, linewidth=1.0)
            top.annotate(f"installed {total:,.0f} MB", xy=(0.995, total),
                         xycoords=("axes fraction", "data"), xytext=(0, 5),
                         textcoords="offset points", ha="right",
                         fontsize=9, color=MUTED)
            top.set_ylim(0, total * 1.10)
        else:
            headroom(top, [s.get(columns[r.key][2], math.nan)
                           for r in curves for s in samples], 1.18)
        view.label_axes(top, y="Memory (MB)")
        tidy(top)
        if len(curves) > 1:
            top.legend(loc="upper left", ncol=len(curves))

    if with_fps:
        ax = panels[-1]
        fx = np.array([p.at for p in data.system_fps], dtype=float)
        fy = np.array([p.value for p in data.system_fps], dtype=float)
        window = stable_smooth(len(fy), data.full_counts.get("system", 0))
        ax.plot(fx, rolling_mean(fy, window) if window else fy,
                color=pal.SLOTS_LIGHT[3], label="System throughput", **LINE_KW)
        ax.set_ylabel("FPS", fontsize=10.5, color=INK_2)
        headroom(ax, fy, 1.12)
        tidy(ax)

    title = "Queue-host RAM over the run"
    xlabel = "seconds into the run"
    panels[-1].set_xlabel(view.xlabel or xlabel)
    panels[-1].set_xlim(float(at[0]), float(at[-1]))
    suptitle(fig, view.titled(title))

    used = np.array([s["used_mb"] for s in samples], dtype=float)
    label = ("host memory, every process on the box"
             if data.broker_source == "ssh" else
             "the broker process alone, not the host"
             if data.broker_source else "source not stated")
    return canvas.save(fig, applied(Chart(
        id="broker_ram_timeline", file="", kind="trend",
        title=title,
        subtitle=f"source={data.broker_source or '?'} — {label}"
                 + (f" · {data.broker_host}" if data.broker_host else ""),
        summary=f"{len(samples)} samples, {used.min():,.0f}–{used.max():,.0f} MB"
                + (f" of {total:,.0f} MB installed" if math.isfinite(total) else "")
                + "; memory climbing while throughput falls is backpressure",
        metrics=["used_mb", "rss_mb", "swap_used_mb"]),
        view, title=title, xlabel=xlabel, ylabel="Memory (MB)", shown=shown), index=16)


# ----------------------------------------------- 17 · queue-host RAM profile
def broker_ram_profile(canvas: Canvas, data: RunData, view: View) -> Chart | None:
    """How full the queue host got, and how much of that the run added.

    Three questions, three panels, deliberately not sharing a y axis: the level
    is hundreds of MB against an installed total, the change across the run is
    usually a fraction of that, and the broker's own RSS answers whether the
    box was full *because of the thing being measured*. One scale would flatten
    two of the three to nothing.

    Percentiles are nearest-rank over the raw samples, so every bar is a
    reading that was actually taken (§2.14).
    """
    used, delta, rabbit = (data.broker.get(k, {}) for k in ("USED", "DELTA", "RABBIT"))
    panels = [
        ("used", "How full it got", S1,
         [("min_mb", "Min"), ("mean_mb", "Mean"), ("p50_mb", "p50"),
          ("p95_mb", "p95"), ("max_mb", "Max")], used),
        ("delta", "What the run added", S2,
         [("start_mb", "Start"), ("end_mb", "End"), ("growth_mb", "Growth"),
          ("peak_over_start_mb", "Peak over start")], delta),
        # RSS answers "is it full *because of the thing I care about*"; swap
        # answers whether the host was already past the point where its latency
        # contribution is stable. Both belong to the box, not to the process,
        # so the panel is titled for what is actually in it.
        ("rabbit", "Broker RSS, and the host's swap", S3,
         [("mean_rss_mb", "Mean RSS"), ("max_rss_mb", "Max RSS"),
          ("swap_max_mb", "Swap max")], rabbit),
    ]
    live = [p for p in panels
            if any(math.isfinite(p[4].get(key, math.nan)) for key, _ in p[3])]
    if not live:
        return None

    shown = Shown(view, [(key, label, color) for key, label, color, _, _ in live])
    drawn = [p for p in live if shown.has(p[0])]
    total = data.broker.get("BROKER", {}).get("total_mb", math.nan)

    fig, axes = plt.subplots(1, len(drawn), squeeze=False,
                             figsize=(4.4 * len(drawn), 4.2))
    for ax, (key, label, color, fields, stats) in zip(axes[0], drawn):
        values = [stats.get(name, math.nan) for name, _ in fields]
        x = np.arange(len(fields), dtype=float)
        bars = ax.bar(x, values, min(0.58, 2.4 / len(fields)), color=color, **BAR_KW)
        label_bars(ax, bars, values, "{:,.0f}")
        ax.set_xticks(x, [text for _, text in fields], fontsize=9.5)
        ax.set_title(label)
        if key == "used" and math.isfinite(total):
            ax.axhline(total, color=AXIS, linewidth=1.0)
            ax.annotate(f"installed {total:,.0f} MB", xy=(0.02, total),
                        xycoords=("axes fraction", "data"), xytext=(0, 5),
                        textcoords="offset points", fontsize=9, color=MUTED)
            ax.set_ylim(0, total * 1.12)
        else:
            headroom(ax, values, 1.20)
        tidy(ax, categorical="x")

    title = "Queue-host RAM profile"
    view.label_axes(axes[0][0], y="Memory (MB)")
    suptitle(fig, view.titled(title))

    swap = rabbit.get("swap_max_mb", math.nan)
    growth = delta.get("growth_mb", math.nan)
    middle = axes[0][len(drawn) // 2]     # centred under the figure, not panel 1
    if math.isfinite(swap) and swap > 0:
        takeaway(middle, f"the host held {swap:,.0f} MB of swap — any latency "
                         "conclusion from this run is unsafe")
    elif math.isfinite(growth):
        takeaway(middle, f"the run left {growth:+,.0f} MB behind"
                         + (" — units are still buffered somewhere"
                            if growth > 0 else " — the drain gave it back"))

    return canvas.save(fig, applied(Chart(
        id="broker_ram_profile", file="", kind="comparison",
        title=title,
        subtitle=f"source={data.broker_source or '?'} · percentiles are "
                 "nearest-rank over the raw samples — each bar is a reading "
                 "that was taken · each panel keeps its own scale",
        summary=(f"peaked at {used.get('max_mb', math.nan):,.0f} MB"
                 + (f" of {total:,.0f} MB" if math.isfinite(total) else "")
                 + (f", {growth:+,.0f} MB across the run"
                    if math.isfinite(growth) else "")),
        metrics=["used_mb", "growth_mb", "rss_mb"]),
        view, title=title, ylabel="Memory (MB)", shown=shown), index=17)


# ------------------------------------------------------------------- tiles
def tiles(data: RunData) -> list[Tile]:
    """C12 — the numbers that *are* the story, not one-bar bar charts.

    Throughput, accuracy, utilization and latency: the four a reader wants
    before any chart, plus the configuration that produced them.
    """
    out: list[Tile] = []

    fps = data.system("fps")
    if math.isfinite(fps):
        share = ", ".join(
            f"{c.split()[-1]}:{data.throughput[c]['share']:.0f}%"
            for c in data.clusters
            if math.isfinite(data.throughput.get(c, {}).get("share", math.nan))
        )
        out.append(Tile(label="System throughput", value=fmt(fps), unit="FPS",
                        source="fps_cluster.log",
                        delta=f"share {share}" if share else ""))

    head = data.accuracy.get((OVERALL, "ALL"))
    if head and math.isfinite(head.get("mAP50", math.nan)):
        out.append(Tile(label="Accuracy (all frames)", value=f"{head['mAP50']:.4f}",
                        unit="mAP@50", source="map.log",
                        delta=f"mAP@50:95 {head.get('mAP50_95', float('nan')):.4f}"))

    system_util = data.utilization.get((SYSTEM, "all"), {})
    pooled = system_util.get("utilization", math.nan)
    if math.isfinite(pooled):
        mean = system_util.get("utilization_mean", math.nan)
        # Pooled and mean diverging means one group is carrying the others (§2.5).
        out.append(Tile(
            label="System utilization", value=f"{pooled:.1f}", unit="%",
            source="utilization_cluster.log",
            delta=(f"mean {mean:.1f}% — imbalanced" if math.isfinite(mean)
                   and abs(mean - pooled) > 5 else
                   f"{fmt(system_util.get('devices'))} devices"),
            delta_kind="bad" if math.isfinite(mean) and abs(mean - pooled) > 5 else ""))

    e2e = data.latency.get((SYSTEM, "all", "e2e"))
    if e2e and math.isfinite(e2e.get("mean_ms", math.nan)):
        out.append(Tile(label="End-to-end latency", value=f"{e2e['mean_ms'] / 1000:.1f}",
                        unit="s mean", source="latency_cluster.log",
                        delta=f"p95 {e2e.get('p95_ms', math.nan) / 1000:.1f}s"))

    # Capacity, not performance: a fleet at 60% free is not slow, it is three
    # times larger than the workload needs. It sits beside utilization rather
    # than instead of it because the two are not complements (§2.10).
    fleet = data.free_time.get((SYSTEM, "all"), {})
    free = fleet.get("free", math.nan)
    if math.isfinite(free):
        mean = fleet.get("free_mean", math.nan)
        uneven = math.isfinite(mean) and abs(mean - free) > 5
        out.append(Tile(
            label="Fleet free time", value=f"{free:.1f}", unit="%",
            source="free_time_cluster.log",
            delta=(f"mean {mean:.1f}% — some devices idle far more than others"
                   if uneven else f"{fmt(fleet.get('devices'))} devices"),
            delta_kind="bad" if uneven else ""))

    peak = data.broker.get("USED", {}).get("max_mb", math.nan)
    if math.isfinite(peak):
        swap = data.broker.get("RABBIT", {}).get("swap_max_mb", math.nan)
        growth = data.broker.get("DELTA", {}).get("growth_mb", math.nan)
        total = data.broker.get("BROKER", {}).get("total_mb", math.nan)
        # The source is part of the number: `ssh` is the host, `rabbitmq_api`
        # is one process, and a tile that reads "1,586 MB" without saying which
        # is a plausible figure meaning something other than its own label.
        out.append(Tile(
            label="Queue-host RAM peak", value=f"{peak:,.0f}", unit="MB",
            source=f"broker_ram.log · source={data.broker_source or '?'}",
            delta=(f"{swap:,.0f} MB swap — latency figures here are unsafe"
                   if math.isfinite(swap) and swap > 0 else
                   f"{growth:+,.0f} MB across the run" if math.isfinite(growth) else
                   f"of {total:,.0f} MB installed" if math.isfinite(total) else ""),
            delta_kind="bad" if math.isfinite(swap) and swap > 0 else ""))

    for key, label, unit in (("batch_size", "Batch size", ""),
                             ("num_bit", "Quantization", "bit"),
                             ("window_batches", "mAP window", "batches")):
        if key in data.config:
            out.append(Tile(label=label, value=fmt(data.config[key]), unit=unit,
                            source="config.yaml"))

    # The tiles are the first numbers anyone reads, which is exactly why each
    # has to say which run it describes. Throughput was recounted from the
    # batch events inside the window; the other three are finished totals the
    # window cannot re-derive. The settings from `config.yaml` are not
    # measurements and need no scope.
    if not data.window.whole:
        recounted = {"fps_cluster.log"} if "throughput" in data.recomputed else set()
        for tile in out:
            if tile.source == "config.yaml":
                continue
            tile.source += (f" · {data.window.label}" if tile.source in recounted
                            else " · whole run")
    return out


# -------------------------------------------------------------------- render
#: The catalogue, in narrative order. `index` lives on each function so a
#: missing input leaves its number unused rather than renumbering the rest.
CATALOGUE: tuple[Callable[[Canvas, RunData, View], Chart | None], ...] = (
    throughput_by_cluster, system_fps_timeline, cluster_fps_timeline,
    fps_distribution, service_latency, e2e_profile, utilization_by_role,
    device_utilization, accuracy_by_window, accuracy_summary,
    # The diagnostics. A run that measured neither optional feature draws the
    # first ten and stops, which is why they come after rather than between.
    queue_wait, device_free_time, free_time_breakdown, machine_free_time,
    free_time_over_run, broker_ram_timeline, broker_ram_profile,
)


#: Charts whose numbers are the window's own, in one of two ways.
#:
#: `WINDOWED` are series: readings the run emitted with a timestamp, filtered
#: to the span. `RECOMPUTED` are summaries the window worked out again from the
#: raw events inside it — the throughput counted off the batch completions, the
#: mAP averaged over the sliding windows that fall inside.
#:
#: Everything else comes from a file holding one finished total that the run
#: never wrote the per-batch series for (see `runlog.WHOLE_RUN_ONLY`). Those
#: keep describing the whole run and say so.
WINDOWED = frozenset({
    "system_window_fps", "cluster_window_fps", "window_fps_distribution",
    "map_by_window", "broker_ram_timeline",
})
RECOMPUTED = frozenset({"throughput_by_cluster"})


def _note_window(chart: Chart, data: RunData) -> None:
    """Stamp the scope onto a chart's subtitle, every chart, both ways round.

    Both ways round is the point. Marking only the narrowed charts would leave
    a reader to assume the unmarked ones were narrowed too -- and "46.9%
    utilization" beside a timeline of the middle 90% of a run is exactly the
    misreading this exists to prevent.

    Every branch reads what actually happened rather than what was meant to:
    a run that wrote no batch events gets no "recomputed" label, and the
    accuracy summary only claims a windowed mean when a windowed mean survived
    to be drawn.
    """
    label = data.window.label
    whole = ("whole run — the run wrote only a finished total for this, "
             f"so the {label} window cannot re-derive it")

    if chart.id == "map_summary":
        # Two columns, and they need not both be there: WINDOW is a mean over
        # the scoring windows inside the span, and a span holding none of them
        # leaves the all-frames figure alone on the chart.
        if any(agg == "WINDOW" for _, agg in data.accuracy):
            scope = (f"the window mean is recomputed over the scoring windows "
                     f"inside {label}; the all-frames figure is the run's own, "
                     "matched frame by frame over everything")
        else:
            scope = whole
    elif chart.id == "free_time_series":
        # A series the window still cannot cut: its offsets are each device's
        # own clock, and the window is a span of the system's. Saying "whole
        # run" alone would read as "the run only wrote a total", which is the
        # wrong reason and would invite someone to go and fix it.
        scope = (f"whole run — these buckets are on each device's own clock, "
                 f"which the {label} window cannot be measured against")
    elif chart.id in RECOMPUTED and "throughput" in data.recomputed:
        scope = f"recomputed over {label} of the run"
    elif chart.id in WINDOWED:
        scope = f"{label} of the run"
    else:
        scope = whole
    chart.subtitle = f"{chart.subtitle} · {scope}" if chart.subtitle else scope


def render(data: RunData, canvas: Canvas,
           views: dict[str, View] | None = None) -> list[str]:
    """Draw every chart the run has the data for. Returns the notes to surface.

    `views` is keyed by chart id. A chart with no entry gets a default `View`,
    which is what an unedited report is.
    """
    notes = list(data.warnings)
    stored = views or {}
    for chart_fn in CATALOGUE:
        try:
            # The id is only known once the chart is built, so the view has to
            # be looked up by the id the function is *going* to use. Keeping the
            # lookup keyed on the function name would break the moment a chart
            # is renamed, so the ids are the contract and live in one place.
            view = stored.get(CHART_IDS[chart_fn.__name__], View())
            chart = chart_fn(canvas, data, view)
            if chart is None:
                log.debug("%s: no data in this run", chart_fn.__name__)
            elif not data.window.whole:
                _note_window(chart, data)
        except Exception as exc:  # noqa: BLE001 - one bad chart must not sink the report
            log.warning("%s failed: %s", chart_fn.__name__, exc, exc_info=True)
            notes.append(f"{chart_fn.__name__.replace('_', ' ')} skipped: {exc}")

    if not data.window.whole:
        low, high = data.window_span or (0.0, data.span_s)
        notes.append(
            f"{data.window.label} window = {low:,.0f}s–{high:,.0f}s of a "
            f"{data.span_s:,.0f}s run, one span for every chart. Throughput is "
            f"recounted from the {len(data.system_batches)} batch(es) that "
            "finished inside it; the FPS series and mAP windows are filtered "
            "to it."
        )
        notes.append(
            "still whole-run, because the run wrote only the finished total: "
            + "; ".join(WHOLE_RUN_ONLY)
            + ". Those charts and the stat tiles are labelled."
        )
    return notes


#: function name -> the chart id it produces. The one mapping between the two,
#: so a stored view can be found before the chart exists.
CHART_IDS: dict[str, str] = {
    "throughput_by_cluster": "throughput_by_cluster",
    "system_fps_timeline": "system_window_fps",
    "cluster_fps_timeline": "cluster_window_fps",
    "fps_distribution": "window_fps_distribution",
    "service_latency": "service_latency_by_role",
    "e2e_profile": "e2e_latency_profile",
    "utilization_by_role": "utilization_by_role",
    "device_utilization": "device_utilization",
    "accuracy_by_window": "map_by_window",
    "accuracy_summary": "map_summary",
    "queue_wait": "queue_wait",
    "device_free_time": "device_free_time",
    "free_time_breakdown": "free_time_breakdown",
    "machine_free_time": "machine_free_time",
    "free_time_over_run": "free_time_series",
    "broker_ram_timeline": "broker_ram_timeline",
    "broker_ram_profile": "broker_ram_profile",
}
