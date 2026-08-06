"""Render a parsed result directory as charts, per `guides/visual_guide.md`.

Two paths meet here.

**A split-inference result directory** — the nine files of
`guides/server_results_guide.md` — is read by `runlog.py` and drawn by
`runcharts.py` as the guide's Part II catalogue. Those files carry `cluster=`,
`role=` and `kind=` on every line, so the charts can compare the things the run
is actually about.

**Anything else** falls through to `parse.py`, which hunts for `name value`
without a schema and can only group by the file a number came from. The forms
below are the honest ones for that: a trend per metric, a spread when several
files measured the same thing, a comparison only when the files are genuinely
alternatives. That last guard matters — grouping by file with no schema is how
you end up charting "`clusters=2` in one log against `clusters=2` in another".

The guide's order of operations is the structure of both paths: pick the form
from the data's job, then assign color by the job it does (`palette`, taken in
slot order), then apply the mark specs (`style`). Color is last.

Charts are PNGs rather than an interactive client-side plot on purpose: a saved
report has to still be readable in six months, and an image is the only
artifact that cannot drift when the UI is rebuilt.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

from . import palette as pal
from . import runcharts, runlog
from .parse import Metric, ParseResult
from .parse import apply_window as clip_parsed
from .style import (
    AXIS, BAD, GOOD, INK, INK_2, LINE_KW, MARK_KW, MUTED, S1, SURFACE, STYLE,
    TINT, BAR_KW, Canvas, Chart, Shown, Tile, View, applied, entity_colors, fmt,
    headroom, label_bars, rolling_mean, smooth_window, suptitle, tidy,
)
from .window import Window

log = logging.getLogger(__name__)

__all__ = ["Chart", "Tile", "View", "Window", "render", "STYLE", "BAR_KW",
           "LINE_KW", "MARK_KW"]

#: A metric needs this many readings before a trend line says anything.
TREND_MIN = 8
#: …and this many before a distribution is worth drawing.
DIST_MIN = 20
#: Lines on one axis past this count get faceted instead (§5).
MAX_LINES = 3

#: Which direction is good, for delta charts (§5). Color keys to the verdict,
#: never to the sign: a +29% latency move is a regression.
LOWER_IS_BETTER = (
    "ms", "latency", "time", "delay", "queue", "depth", "loss", "error", "err",
    "drop", "power", "energy", "temp", "memory", "mem", "size", "bytes", "mb",
)
HIGHER_IS_BETTER = (
    "fps", "throughput", "accuracy", "map", "precision", "recall", "score",
    "speed", "rate", "bandwidth", "iou", "frames",
)


# ------------------------------------------------------------------- helpers
def axis_label(metric: Metric) -> str:
    return f"{metric.label} ({metric.unit})" if metric.unit else metric.label


def short_source(name: str) -> str:
    """`logs/edge/run.log` -> `edge/run.log`: enough to tell files apart."""
    parts = name.split("/")
    return "/".join(parts[-2:]) if len(parts) > 1 else name


def goal_of(name: str) -> int:
    """+1 higher is better, -1 lower is better, 0 neither."""
    text = name.lower()
    for token in HIGHER_IS_BETTER:
        if token in text:
            return 1
    for token in LOWER_IS_BETTER:
        if token in text:
            return -1
    return 0


def _scale_panels(scales: list[float], limit: float = 50.0) -> list[list[int]]:
    """Group metric indices so no panel spans more than `limit`x in magnitude.

    Two measures of very different scale on one linear axis flattens the
    smaller to nothing; the guide's answer is more panels, never a second
    y-scale (§1). Capped at three panels -- past that the chart is a table.
    """
    order = sorted(range(len(scales)), key=lambda i: scales[i])
    panels: list[list[int]] = []
    for i in order:
        if panels:
            current = [scales[j] for j in panels[-1]] + [scales[i]]
            if max(current) / max(min(current), 1e-9) <= limit or len(panels) >= 3:
                panels[-1].append(i)
                continue
        panels.append([i])
    return [sorted(panel) for panel in panels]


# ------------------------------------------------- generic path: chart bodies
#: A clock is only an x-axis if it actually resolves the readings. Logs print
#: whole seconds far more often than not, so a 20 Hz loop lands ten samples on
#: one tick -- plotted against time that draws vertical spikes and a line that
#: doubles back on itself, which reads as wild instability that is not there.
TIME_RESOLUTION = 0.6


def _series_for(metric: Metric) -> list[tuple[str, np.ndarray, np.ndarray, bool]]:
    """(source, x, y, x_is_time) per file, in the order the log printed them."""
    out = []
    for source, samples in metric.by_source.items():
        rows = sorted(samples, key=lambda s: (s.line, s.order))
        y = np.array([s.value for s in rows], dtype=float)
        times = [s.at for s in rows]
        resolved = (
            len(rows) > 1
            and all(t is not None for t in times)
            and len(set(times)) >= TIME_RESOLUTION * len(times)
        )
        if resolved:
            out.append((source, np.array(times, dtype=float), y, True))
        else:
            out.append((source, np.arange(len(rows), dtype=float), y, False))
    return out


def _drop_identical(
    series: list[tuple[str, np.ndarray, np.ndarray, bool]]
) -> tuple[list[tuple[str, np.ndarray, np.ndarray, bool]], list[str]]:
    """Guide §III.4: two runs with identical values must not be drawn as two lines.

    Overlapping them implies a comparison that is not in the data, so the
    duplicate is dropped and said out loud in the caption instead.
    """
    kept: list[tuple[str, np.ndarray, np.ndarray, bool]] = []
    notes: list[str] = []
    for name, x, y, timed in series:
        twin = next(
            (k for k, _, ky, _ in kept if len(ky) == len(y) and bool(np.all(ky == y))),
            None,
        )
        if twin is not None:
            notes.append(f"{short_source(name)} is identical to {short_source(twin)}")
            continue
        kept.append((name, x, y, timed))
    return kept, notes


def _trend(canvas: Canvas, metric: Metric, view: View) -> Chart | None:
    """Change over time -> lines, one per file, faceted past three (§5)."""
    series = [s for s in _series_for(metric) if len(s[2]) >= TREND_MIN]
    if not series:
        return None
    series, notes = _drop_identical(series)
    colors = entity_colors([s[0] for s in series[: len(pal.SLOTS_LIGHT)]])
    shown = Shown(view, [(s[0], short_source(s[0]), colors.get(s[0], S1))
                         for s in series[: len(pal.SLOTS_LIGHT)]])
    keys = {r.key for r in shown}
    series = [s for s in series if s[0] in keys]
    if not series:
        return None
    # Only call it a clock if every series has one that resolves its readings.
    timed = all(is_time for *_, is_time in series)
    xlabel = "seconds into the run" if timed else "reading"
    title = f"{metric.label} over the run"
    ylabel = axis_label(metric)

    if len(series) <= MAX_LINES:
        fig, ax = plt.subplots(figsize=(9.0, 4.1))
        single = len(series) == 1
        for source, x, y, _ in series:
            color = colors[source]
            # A single noisy series reads as a scribble. The rolling mean is
            # the same entity, so it wears the same hue at full strength and
            # the raw readings drop to the pale end of that hue's ramp -- not
            # to a faded version of the slot, which would shift toward the
            # surface and break the contrast the palette was validated for.
            window = smooth_window(len(y)) if single else 0
            if window:
                ax.plot(x, y, color=TINT, label="reading", linewidth=1.4,
                        solid_capstyle="round")
                ax.plot(x, rolling_mean(y, window), color=color,
                        label=f"{window}-reading mean", **LINE_KW)
            else:
                ax.plot(x, y, color=color, label=short_source(source), **LINE_KW)

        # Direct-label selectively: the endpoint of the highest-ending series,
        # which is the one a reader is trying to name (§1, §5).
        top = max(series, key=lambda s: s[2][-1])
        top_color = colors[top[0]]
        ax.plot([top[1][-1]], [top[2][-1]], "o", color=top_color, **MARK_KW)
        ax.annotate(
            fmt(float(top[2][-1])),
            xy=(top[1][-1], top[2][-1]), xytext=(7, 0), textcoords="offset points",
            color=top_color, fontsize=9.5, fontweight="bold", va="center",
        )
        # Room on the right for that label, so `bbox_inches="tight"` does not
        # have to grow the figure to fit text hanging past the axes.
        left = min(float(x[0]) for _, x, _, _ in series)
        right = max(float(x[-1]) for _, x, _, _ in series)
        ax.set_xlim(left, right + (right - left or 1.0) * 0.06)
        headroom(ax, np.concatenate([y for _, _, y, _ in series]), 1.12)
        if len(series) >= 2 or ax.get_legend_handles_labels()[0][1:]:
            ax.legend(loc="lower right", ncol=2)
        view.label_axes(ax, x=xlabel, y=ylabel)
        ax.set_title(view.titled(title))
        tidy(ax)
    else:
        # Small multiples: every panel carries one series, so no color is
        # encoding identity and the three-slot all-pairs cap cannot be breached.
        cols = min(3, len(series))
        rows = math.ceil(len(series) / cols)
        fig, axes = plt.subplots(rows, cols, figsize=(4.0 * cols, 2.7 * rows),
                                 sharey=True, squeeze=False)
        for ax, (source, x, y, _) in zip(axes.flat, series):
            ax.plot(x, y, color=S1, **LINE_KW)
            ax.set_title(short_source(source), fontsize=10, color=INK_2, pad=6)
            tidy(ax)
        for ax in list(axes.flat)[len(series):]:
            ax.set_visible(False)
        fig.supylabel(view.ylabel or ylabel, fontsize=10.5, color=INK_2)
        fig.supxlabel(view.xlabel or xlabel, fontsize=10.5, color=INK_2)
        suptitle(fig, view.titled(title))

    values = np.concatenate([y for _, _, y, _ in series])
    summary = (
        f"{len(values)} readings across {len(series)} file(s); "
        f"mean {fmt(float(values.mean()))}{metric.unit}, "
        f"range {fmt(float(values.min()))}–{fmt(float(values.max()))}{metric.unit}"
    )
    return canvas.save(
        fig,
        applied(Chart(id=f"trend_{metric.name}", file="", kind="trend", title=title,
                       subtitle=" · ".join(notes), summary=summary,
                       metrics=[metric.name]),
                 view, title=title, xlabel=xlabel, ylabel=ylabel, shown=shown),
    )


def _distribution(canvas: Canvas, metric: Metric, view: View) -> Chart | None:
    """Spread and outliers -> boxplot per file (§5 boxplot rules)."""
    groups = [
        (source, np.array([s.value for s in rows], dtype=float))
        for source, rows in metric.by_source.items()
        if len(rows) >= TREND_MIN
    ]
    if len(groups) < 2 or sum(len(v) for _, v in groups) < DIST_MIN:
        return None
    if len(groups) > len(pal.SLOTS_LIGHT):
        groups = sorted(groups, key=lambda g: -len(g[1]))[: len(pal.SLOTS_LIGHT)]

    colors = entity_colors([s for s, _ in groups])
    # A box per file needs two files to be a comparison, so a hidden set that
    # would leave one is ignored rather than dropping the chart.
    shown = Shown(view, [(s, short_source(s), colors[s]) for s, _ in groups], minimum=2)
    keys = {r.key for r in shown}
    groups = [g for g in groups if g[0] in keys]
    fig, ax = plt.subplots(figsize=(max(6.0, 1.5 * len(groups) + 2.5), 4.0))
    box = ax.boxplot(
        [v for _, v in groups],
        patch_artist=True,
        widths=0.5,
        tick_labels=[short_source(s) for s, _ in groups],
        medianprops=dict(color=SURFACE, linewidth=1.6),
        whiskerprops=dict(color=AXIS, linewidth=1.0),
        capprops=dict(color=AXIS, linewidth=1.0),
        flierprops=dict(marker="o", markersize=3.5, markerfacecolor=MUTED,
                        markeredgecolor=SURFACE, alpha=0.7),
    )
    for patch, (source, _) in zip(box["boxes"], groups):
        patch.set_facecolor(colors[source])
        patch.set_edgecolor(SURFACE)
        patch.set_linewidth(1.2)

    # Annotate above the whisker cap, never at p75 -- p75 sits inside the
    # whisker and the label collides with it (§5).
    tops = []
    for i, (_, values) in enumerate(groups, start=1):
        q1, q3 = np.percentile(values, [25, 75])
        whisker_top = values[values <= q3 + 1.5 * (q3 - q1)].max()
        tops.append(whisker_top)
        ax.annotate(
            f"median {fmt(float(np.median(values)))}",
            xy=(i, whisker_top), xytext=(0, 8), textcoords="offset points",
            ha="center", fontsize=9, color=INK_2,
        )
    headroom(ax, tops)
    title, ylabel = f"{metric.label} — spread by file", axis_label(metric)
    view.label_axes(ax, y=ylabel)
    ax.set_title(view.titled(title))
    tidy(ax, categorical="x")

    spreads = ", ".join(
        f"{short_source(s)} median {fmt(float(np.median(v)))}{metric.unit}"
        for s, v in groups
    )
    return canvas.save(
        fig,
        applied(Chart(id=f"dist_{metric.name}", file="", kind="distribution",
                      title=title, summary=spreads, metrics=[metric.name]),
                view, title=title, ylabel=ylabel, shown=shown),
    )


def _comparison(canvas: Canvas, metrics: list[Metric], sources: list[str],
                view: View) -> Chart | None:
    """One reading per file -> grouped bars, direct-labelled (§5)."""
    if len(sources) < 2 or not metrics:
        return None
    metrics = metrics[:6]
    # One slot per file, never a generated hue: past the eighth the tail is
    # dropped and said out loud rather than recoloured (§1).
    dropped = sources[len(pal.SLOTS_LIGHT):]
    sources = sources[: len(pal.SLOTS_LIGHT)]
    colors = entity_colors(sources)
    shown = Shown(view, [(s, short_source(s), colors[s]) for s in sources], minimum=2)
    sources = [r.key for r in shown]
    width = 0.36 if len(sources) == 2 else min(0.36, 0.8 / len(sources))
    offs = (np.arange(len(sources)) - (len(sources) - 1) / 2) * (width + 0.03)

    scale = [max(abs(v) for v in m.values) or 1.0 for m in metrics]
    panels = _scale_panels(scale)
    split = len(panels) > 1

    fig, axes = plt.subplots(
        1, len(panels), figsize=(max(6.4, 2.0 * len(metrics) + 2.0), 4.1), squeeze=False
    )
    for ax, indices in zip(axes[0], panels):
        local = np.arange(len(indices), dtype=float)
        heights: list[float] = []
        for si, source in enumerate(sources):
            values = [
                next((s.value for s in metrics[i].by_source.get(source, [])), math.nan)
                for i in indices
            ]
            heights.extend(v for v in values if math.isfinite(v))
            bars = ax.bar(local + offs[si], values, width,
                          color=colors[source], label=short_source(source), **BAR_KW)
            # Slots 3-5 fall under 3:1 on this surface, so every bar carries a
            # visible label rather than relying on the fill to be readable (§2).
            label_bars(ax, bars, values, "{:.4g}")
        ax.set_xticks(local)
        ax.set_xticklabels([metrics[i].label for i in indices], fontsize=9.5)
        headroom(ax, heights)
        tidy(ax, categorical="x")
    units = {m.unit for m in metrics if m.unit}
    title = "Per-file comparison"
    ylabel = next(iter(units)) if len(units) == 1 else "value"
    view.label_axes(axes[0][0], y=ylabel)
    # Below the axes: the upper corners belong to the tallest bar (§5).
    axes[0][0].legend(loc="upper center", bbox_to_anchor=(0.5, -0.11),
                      ncol=min(4, len(sources)))
    suptitle(fig, view.titled(title))

    return canvas.save(
        fig,
        applied(Chart(id="comparison", file="", kind="comparison", title=title,
              subtitle="; ".join(filter(None, [
                  f"{len(panels)} panels — the metrics differ too much in "
                  "scale to share one axis" if split else "",
                  f"{len(dropped)} further file(s) not shown" if dropped else "",
              ])),
              summary=f"{len(metrics)} metric(s) across {len(sources)} files",
              metrics=[m.name for m in metrics]),
                view, title=title, ylabel=ylabel, shown=shown),
    )


def _delta(canvas: Canvas, metrics: list[Metric], sources: list[str],
           view: View) -> Chart | None:
    """Exactly two files -> percent change, coloured by verdict not by sign (§5)."""
    if len(sources) != 2:
        return None
    a, b = sources
    # A row per metric, so the metrics are what the config panel offers. The
    # swatch is left empty: these bars are coloured by verdict, not identity,
    # and a fixed chip beside the name would claim otherwise.
    shown = Shown(view, [(m.name, m.label, "") for m in metrics[:8]])
    keys = {r.key for r in shown}
    rows: list[tuple[str, float, int]] = []
    for metric in [m for m in metrics[:8] if m.name in keys]:
        first = next((s.value for s in metric.by_source.get(a, [])), None)
        second = next((s.value for s in metric.by_source.get(b, [])), None)
        if first in (None, 0) or second is None:
            continue
        rows.append((metric.label, (second - first) / abs(first) * 100.0,
                     goal_of(metric.name)))
    # A delta needs something that actually moved. One metric that did not is
    # a bar of length zero and a headline that invents a comparison.
    if len(rows) < 2 and not any(abs(p) > 0.01 for _, p, _ in rows):
        return None

    labels = [r[0] for r in rows]
    pct = [r[1] for r in rows]
    scores = [goal * (1 if p > 0 else -1 if p < 0 else 0) for _, p, goal in rows]
    colors = [GOOD if s > 0 else BAD if s < 0 else MUTED for s in scores]
    verdicts = ["better" if s > 0 else "worse" if s < 0 else "neutral" for s in scores]

    fig, ax = plt.subplots(figsize=(8.6, max(2.8, 0.55 * len(rows) + 1.7)))
    y = np.arange(len(rows))
    ax.barh(y, pct, height=0.5, color=colors, **BAR_KW)
    ax.axvline(0, color=AXIS, linewidth=1.0)
    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontsize=9.5)
    ax.tick_params(axis="y", colors=INK_2)
    ax.invert_yaxis()
    span = max(abs(p) for p in pct) or 1.0
    for i, (p, verdict) in enumerate(zip(pct, verdicts)):
        # The verdict is spelled out: the reading must never rest on color (§5).
        ax.annotate(
            f"{p:+.1f}%  {verdict}",
            xy=(p, i), xytext=(6 if p >= 0 else -6, 0), textcoords="offset points",
            ha="left" if p >= 0 else "right", va="center",
            fontsize=9.5, fontweight="semibold", color=INK,
        )
    # Symmetric about zero, or the bar lengths misrepresent the ratio (§C11).
    ax.set_xlim(-span * 1.6, span * 1.6)
    title = "Change by metric — coloured by verdict, not by sign"
    xlabel = f"change from {short_source(a)} to {short_source(b)} (%)"
    view.label_axes(ax, x=xlabel)
    ax.set_title(view.titled(title))
    # A diverging bar chart's reference is the zero line, not the axis (§C11).
    tidy(ax, categorical="y", hide=("top", "right", "left"))

    worse = [labels[i] for i, s in enumerate(scores) if s < 0]
    better = [labels[i] for i, s in enumerate(scores) if s > 0]
    summary = (
        f"{short_source(b)} vs {short_source(a)}: "
        + (f"{len(better)} better" if better else "none better")
        + (f", {len(worse)} worse ({', '.join(worse[:3])})" if worse else ", none worse")
    )
    return canvas.save(
        fig,
        applied(Chart(id="delta", file="", kind="delta", title=title,
                      subtitle=f"{short_source(a)} → {short_source(b)}",
                      summary=summary, metrics=[m.name for m in metrics[:8]]),
                view, title=title, xlabel=xlabel, shown=shown),
    )


def _stage_breakdown(canvas: Canvas, result: ParseResult, view: View) -> Chart | None:
    """Split-inference's own shape: edge / transfer / cloud on one ms axis."""
    wanted = [
        ("edge", "edge_ms"), ("transfer", "transfer_ms"),
        ("cloud", "cloud_ms"), ("end-to-end", "e2e_ms"),
    ]
    found = [
        (label, result.metrics[name])
        for label, name in wanted
        if name in result.metrics and result.metrics[name].values
    ]
    if len(found) < 2:
        return None

    # Sequential ramp: these are magnitudes of one quantity, not identities,
    # so one hue light->dark rather than categorical slots (§2). The ramp is
    # indexed over the *declared* stages, so hiding one does not re-shade the
    # rest -- the same rule as an entity dict, for an ordered scale (§1).
    ramp = pal.SEQUENTIAL[1:]
    shade = {label: ramp[min(i, len(ramp) - 1)] for i, (label, _) in enumerate(found)}
    # Two stages minimum: one stage left visible is the one-bar bar chart the
    # guide says is never the right form (§0).
    shown = Shown(view, [(label, label, shade[label]) for label, _ in found], minimum=2)
    keys = {r.key for r in shown}
    found = [f for f in found if f[0] in keys]

    labels = [label for label, _ in found]
    means = [float(np.mean(m.values)) for _, m in found]
    colors = [shade[label] for label in labels]

    fig, ax = plt.subplots(figsize=(6.6, 3.9))
    bars = ax.bar(labels, means, width=0.5, color=colors, **BAR_KW)
    label_bars(ax, bars, means, "{:,.0f} ms", fontsize=9)
    headroom(ax, means)
    title, ylabel = "Where the time goes", "mean latency (ms)"
    view.label_axes(ax, y=ylabel)
    ax.set_title(view.titled(title))
    tidy(ax, categorical="x")

    total = sum(m for label, m in zip(labels, means) if label != "end-to-end")
    dominant = labels[int(np.argmax(means))]
    summary = (
        f"{dominant} dominates at {fmt(max(means))} ms"
        + (f"; stages sum to {fmt(total)} ms" if total else "")
    )
    return canvas.save(
        fig,
        applied(Chart(id="stage_breakdown", file="", kind="breakdown",
                      title=title, summary=summary,
                      metrics=[m.name for _, m in found]),
                view, title=title, ylabel=ylabel, shown=shown),
    )


# ------------------------------------------------------------- generic plan
def _shared_scalars(ranked: list[Metric], sources: list[str]) -> tuple[list[Metric], list[str]]:
    """Metrics that several files each report once — the comparable ones.

    A metric only counts if the files actually disagree about it. Two logs both
    printing `clusters=2` describe the same fact from two angles, not two
    alternatives worth a bar each; charting them produces a pair of identical
    bars and a `+0.0%` delta that says nothing.
    """
    shared = [
        m for m in ranked
        if m.is_scalar and len(m.by_source) >= 2 and m.spread > 0
    ]
    compare_sources = sorted(
        {s for m in shared for s in m.by_source},
        key=lambda s: sources.index(s) if s in sources else 0,
    )
    return shared, compare_sources


def _generic(result: ParseResult, canvas: Canvas, max_charts: int,
             views: dict[str, View]) -> tuple[list[Tile], list[str]]:
    """The no-schema path: draw what a directory nobody has seen turned out to hold."""
    notes: list[str] = []
    tiles: list[Tile] = []
    ranked = result.ranked()
    sources = [f.path for f in result.read_files]

    def view(chart_id: str) -> View:
        return views.get(chart_id, View())

    try:
        _stage_breakdown(canvas, result, view("stage_breakdown"))
    except Exception as exc:  # noqa: BLE001 - one bad chart must not sink the report
        log.warning("stage breakdown failed: %s", exc)
        notes.append(f"stage breakdown skipped: {exc}")

    shared, compare_sources = _shared_scalars(ranked, sources)
    for chart_id, chart_fn in (("comparison", _comparison), ("delta", _delta)):
        try:
            chart_fn(canvas, shared, compare_sources, view(chart_id))
        except ValueError as exc:
            notes.append(str(exc))
        except Exception as exc:  # noqa: BLE001
            log.warning("%s failed: %s", chart_fn.__name__, exc)

    for metric in ranked:
        if len(canvas.charts) >= max_charts:
            notes.append(
                f"stopped at {max_charts} charts; "
                f"{len(ranked)} metrics were found in total"
            )
            break
        if metric.is_scalar:
            continue
        try:
            drawn = _trend(canvas, metric, view(f"trend_{metric.name}"))
            if drawn and len(canvas.charts) < max_charts:
                _distribution(canvas, metric, view(f"dist_{metric.name}"))
        except ValueError as exc:
            notes.append(f"{metric.label}: {exc}")
        except Exception as exc:  # noqa: BLE001
            log.warning("chart for %s failed: %s", metric.name, exc)
            notes.append(f"{metric.label}: chart failed ({exc})")

    for metric in ranked:
        if metric.is_scalar and len(metric.by_source) == 1:
            source, rows = next(iter(metric.by_source.items()))
            tiles.append(
                Tile(label=metric.label, value=fmt(rows[0].value),
                     unit=metric.unit, source=short_source(source))
            )
    return tiles[:12], notes


# ----------------------------------------------------------------- the plan
def render(
    result: ParseResult,
    out_dir: Path,
    *,
    source_dir: Path | None = None,
    max_charts: int = 10,
    views: dict[str, View] | None = None,
    window: Window | None = None,
) -> tuple[list[Chart], list[Tile], list[str]]:
    """Pick the forms, draw them, and return (charts, tiles, notes).

    `source_dir` is the directory the logs were pulled into. When it is one of
    the server's result directories the catalogue runs against the real schema;
    otherwise the generic path draws what the parser managed to find.

    `views` are the per-chart overrides an operator set in the UI, keyed by
    chart id. Applying them means drawing the whole report again rather than
    editing a PNG, which is the point: the saved image *is* the edited chart.

    `window` narrows the run to a fixed a%–b% slice of its readings before any
    of that happens, so every chart in the report is drawn from the same
    stretch. It is applied to the *data*, never to a finished figure — a
    windowed chart's axes, mean line and endpoint label all describe the
    window, which is the only reading of them that is true.

    Form comes from the data's job, per the guide's §0: many readings over a
    run is change-over-time, one reading per file is a comparison, and a single
    number is a stat tile rather than a one-bar bar chart.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    canvas = Canvas(out_dir)
    tiles: list[Tile] = []
    notes: list[str] = []
    stored = views or {}
    slice_ = window or Window()

    known = source_dir is not None and runlog.detect(source_dir)
    with plt.rc_context(STYLE):
        if known:
            assert source_dir is not None
            data = runlog.read_run(source_dir, slice_)
            notes = runcharts.render(data, canvas, stored)
            tiles = runcharts.tiles(data)
            if not canvas.charts:
                notes.append(
                    "the result files were present but empty — falling back to "
                    "reading them without a schema"
                )
                known = False
        if not known:
            windowed = clip_parsed(result, slice_)
            tiles, generic_notes = _generic(windowed, canvas, max_charts, stored)
            notes.extend(generic_notes)
            if not slice_.whole:
                notes.append(
                    f"{slice_.label} window: these files share no run clock — "
                    "nothing here says when a reading was taken relative to "
                    "anything else — so each metric was cut to that fraction of "
                    "its own readings instead, and a value a file states once "
                    "was kept whole"
                )

    if not canvas.charts:
        notes.append(
            "nothing chartable: the logs held single readings only, "
            "which are shown as tiles above"
        )
    return canvas.charts, tiles, notes
