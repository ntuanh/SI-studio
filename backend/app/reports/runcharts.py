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
| *(derived)* | the stat tiles (C12) |

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

from . import palette as pal
from .runlog import OVERALL, SYSTEM, Point, RunData
from .style import (
    AXIS, INK_2, LINE_KW, MARKER_MAX, MARK_KW, MUTED, S1, S2, SURFACE,
    TINT, BAR_KW, Canvas, Chart, Shown, Tile, View, band, entity_colors, fmt,
    applied, grouped_x, headroom, label_bars, offsets, panel_legend,
    rolling_mean, smooth_window, suptitle, takeaway, tidy,
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
    window = smooth_window(len(y))

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
    smoothing = max(smooth_window(len(p)) for _, p in series)

    def draw(ax: plt.Axes, cluster: str, points: Sequence[Point], color: str) -> float:
        px, py = _xy(points)
        win = smooth_window(len(py))
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

    # Number WITHIN each role: a running counter gives `C1 C2 E3 E4`, which
    # reads as two missing devices (§C8).
    seen: dict[str, int] = {}
    ticks = []
    for d in devices:
        role = str(d["role"])
        seen[role] = seen.get(role, 0) + 1
        ticks.append(f"{role[:1].upper()}{seen[role]}")

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

    for key, label, unit in (("batch_size", "Batch size", ""),
                             ("num_bit", "Quantization", "bit"),
                             ("window_batches", "mAP window", "batches")):
        if key in data.config:
            out.append(Tile(label=label, value=fmt(data.config[key]), unit=unit,
                            source="config.yaml"))

    # Every measured tile above is a summary line the run computed over its own
    # full duration, so an analysis window leaves them untouched. They sit at
    # the top of the report, which is precisely why they have to say so — the
    # settings from `config.yaml` are not measurements and need no scope.
    if not data.window.whole:
        for tile in out:
            if tile.source != "config.yaml":
                tile.source += " · whole run"
    return out


# -------------------------------------------------------------------- render
#: The catalogue, in narrative order. `index` lives on each function so a
#: missing input leaves its number unused rather than renumbering the rest.
CATALOGUE: tuple[Callable[[Canvas, RunData, View], Chart | None], ...] = (
    throughput_by_cluster, system_fps_timeline, cluster_fps_timeline,
    fps_distribution, service_latency, e2e_profile, utilization_by_role,
    device_utilization, accuracy_by_window, accuracy_summary,
)


#: Charts drawn from a per-batch series, which is what an a%–b% window can
#: actually narrow. Everything else in the catalogue comes from a file holding
#: one summary line per cluster that the *run* computed over its whole
#: duration; those numbers cannot be re-derived for a slice of it, so they keep
#: describing the whole run and say so. See `window.py`.
WINDOWED = frozenset({
    "system_window_fps", "cluster_window_fps", "window_fps_distribution",
    "map_by_window",
})


def _note_window(chart: Chart, window: Window) -> None:
    """Stamp the scope onto a chart's subtitle, both ways round.

    Both ways round is the point. Marking only the windowed charts would leave
    a reader to assume the unmarked ones were windowed too -- and "throughput
    25.2 FPS" beside a timeline of batches 5-90 is exactly the misreading this
    exists to prevent.
    """
    scope = (
        f"{window.label} of the run" if chart.id in WINDOWED
        else f"whole run — this figure is the run's own summary line, which the "
             f"{window.label} window cannot narrow"
    )
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
                _note_window(chart, data.window)
        except Exception as exc:  # noqa: BLE001 - one bad chart must not sink the report
            log.warning("%s failed: %s", chart_fn.__name__, exc, exc_info=True)
            notes.append(f"{chart_fn.__name__.replace('_', ' ')} skipped: {exc}")

    if not data.window.whole:
        narrowed = [c.title for c in canvas.charts if c.id in WINDOWED]
        notes.append(
            f"{data.window.label} window: cut from the per-batch logs, so "
            + (", ".join(narrowed) if narrowed else "no chart in this run")
            + " covers that slice. The throughput, latency, utilization and "
              "accuracy summaries — and every stat tile — are the run's own "
              "whole-run figures and are labelled as such."
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
}
