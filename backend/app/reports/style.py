"""The visual guide's §4 style block, and the primitives every chart uses.

One copy, imported by both chart modules. `charts.py` draws whatever an unknown
directory turned out to hold; `runcharts.py` draws the guide's Part II
catalogue for a split-inference result directory. They must not drift into two
looks, so the rcParams, the mark specs and the save/tidy helpers live here.

Everything is drawn on the **light** surface in both UI themes: `#fcfcfb` is
the surface the guide's contrast floors were computed against, and a PNG keeps
whatever theme it was drawn in long after the page around it has changed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

import matplotlib

matplotlib.use("Agg")  # no display on a server; must precede the pyplot import

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from . import palette as pal  # noqa: E402

SURFACE = pal.CHROME["light"]["surface"]
INK = pal.CHROME["light"]["ink"]
INK_2 = pal.CHROME["light"]["ink_2"]
MUTED = pal.CHROME["light"]["muted"]
GRID = pal.CHROME["light"]["grid"]
AXIS = pal.CHROME["light"]["axis"]

#: Categorical slots 1-3, the ones every chart here reaches for first (§2).
S1, S2, S3 = pal.SLOTS_LIGHT[0], pal.SLOTS_LIGHT[1], pal.SLOTS_LIGHT[2]
GOOD, BAD = pal.STATUS["good"], pal.STATUS["critical"]
#: The pale end of the sequential ramp. This is what a raw series wears when a
#: rolling mean is drawn over it -- same hue, no alpha. Fading a line with
#: `alpha` shifts it toward the surface and quietly breaks the contrast check.
TINT = pal.SEQUENTIAL[1]

#: Guide §4, verbatim. Solid hairline grid -- dashed reads as "threshold".
STYLE: dict[str, object] = {
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
    "savefig.facecolor": SURFACE,
    "font.family": ["Segoe UI", "DejaVu Sans", "sans-serif"],
    "font.size": 10,
    "axes.titlesize": 13, "axes.titleweight": "semibold",
    "axes.titlecolor": INK, "axes.titlepad": 12,
    "axes.labelsize": 10.5, "axes.labelcolor": INK_2,
    "axes.edgecolor": AXIS, "axes.linewidth": 0.8,
    "axes.grid": True, "axes.axisbelow": True,
    "grid.color": GRID, "grid.linestyle": "-", "grid.linewidth": 0.8,
    "xtick.color": MUTED, "ytick.color": MUTED,
    "xtick.labelsize": 9.5, "ytick.labelsize": 9.5,
    "xtick.major.size": 0, "ytick.major.size": 0,
    "legend.frameon": False, "legend.fontsize": 9.5, "legend.labelcolor": INK_2,
    "figure.dpi": 110, "savefig.dpi": 300, "savefig.bbox": "tight",
}

#: The surface-coloured edge IS the 2px gap between adjacent fills. It is not a
#: contrasting border drawn to separate marks -- never black (§1).
BAR_KW = dict(edgecolor=SURFACE, linewidth=1.2)
LINE_KW = dict(linewidth=2.0, solid_capstyle="round")
MARK_KW = dict(markersize=6, markeredgecolor=SURFACE, markeredgewidth=1.4)

#: Sparse series get markers, dense ones do not (§5).
MARKER_MAX = 30


# ------------------------------------------------------------------- outputs
@dataclass
class SeriesRef:
    """One toggleable thing in a chart, as the config panel needs to list it.

    `key` is stable across re-renders and is what a hidden-set refers to;
    `label` is what the legend says, which may carry a computed number
    (`31-reading mean`) and so cannot be an identifier.
    """

    key: str
    label: str
    color: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"key": self.key, "label": self.label, "color": self.color}


@dataclass
class View:
    """What an operator changed about a chart: its wording, and what it shows.

    The charts are PNGs drawn on the server, so this is not a client-side
    toggle -- the overrides are stored with the report and the figure is drawn
    again. That is the price of an artifact that still reads in six months, and
    it buys one thing worth having: the *saved* chart is the edited chart, so a
    renamed axis is in the file itself rather than in a viewer's local state.
    """

    title: str = ""
    xlabel: str = ""
    ylabel: str = ""
    hidden: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: object) -> "View":
        if not isinstance(data, dict):
            return cls()
        hidden = data.get("hidden") or ()
        return cls(
            title=str(data.get("title") or "").strip()[:160],
            xlabel=str(data.get("xlabel") or "").strip()[:120],
            ylabel=str(data.get("ylabel") or "").strip()[:120],
            hidden=tuple(str(h) for h in hidden if str(h)) if isinstance(hidden, (list, tuple)) else (),
        )

    def to_dict(self) -> dict[str, object]:
        return {"title": self.title, "xlabel": self.xlabel,
                "ylabel": self.ylabel, "hidden": list(self.hidden)}

    @property
    def empty(self) -> bool:
        return not (self.title or self.xlabel or self.ylabel or self.hidden)

    def visible(self, keys: Sequence[str], minimum: int = 1) -> list[str]:
        """The keys still to be drawn.

        A hidden set that would leave fewer than `minimum` series is ignored
        outright. Hiding everything would draw an empty frame with a title and
        leave no way back from the UI; and some forms have a real floor -- a
        two-stage breakdown with one stage hidden is the one-bar bar chart the
        guide is explicit about never drawing (§0). Dropping the chart instead
        is worse: the card vanishes with nothing to say why.
        """
        kept = [k for k in keys if k not in self.hidden]
        return kept if len(kept) >= min(max(minimum, 1), len(keys)) else list(keys)

    def titled(self, default: str) -> str:
        return self.title or default

    def label_axes(self, ax: plt.Axes, *, x: str = "", y: str = "") -> None:
        if self.xlabel or x:
            ax.set_xlabel(self.xlabel or x)
        if self.ylabel or y:
            ax.set_ylabel(self.ylabel or y)


class Shown:
    """What a chart declares it can draw, and what this view leaves drawn.

    Every chart body builds one of these instead of iterating a series list
    directly. It keeps two things that must not drift apart: the full list the
    config panel offers, and the filtered list the figure actually gets.
    """

    def __init__(self, view: View, refs: Sequence[tuple[str, str, str]],
                 minimum: int = 1) -> None:
        self.all = [SeriesRef(key, label, color) for key, label, color in refs]
        keys = set(view.visible([r.key for r in self.all], minimum))
        self.drawn = [r for r in self.all if r.key in keys]

    def __iter__(self):
        return iter(self.drawn)

    def __len__(self) -> int:
        return len(self.drawn)

    def __bool__(self) -> bool:
        return bool(self.drawn)

    def has(self, key: str) -> bool:
        return any(r.key == key for r in self.drawn)

    @property
    def labels(self) -> list[str]:
        return [r.label for r in self.drawn]


@dataclass
class Chart:
    id: str
    file: str
    title: str
    kind: str
    subtitle: str = ""
    summary: str = ""
    metrics: list[str] = field(default_factory=list)
    #: A timeline or a multi-panel figure needs the full width of the gallery
    #: to read; a single-panel bar chart does not. Set from the figure itself
    #: at save time, so the layout hint can never disagree with the drawing.
    wide: bool = False
    #: Everything the chart *can* show, drawn or not -- the config panel lists
    #: these, so a series hidden last time is still there to switch back on.
    series: list[SeriesRef] = field(default_factory=list)
    #: The wording this chart would use with no overrides. The UI shows them as
    #: placeholders, so "reset" is visible without being guessed at.
    defaults: dict[str, str] = field(default_factory=dict)
    #: The overrides actually applied.
    view: View = field(default_factory=View)

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id, "file": self.file, "title": self.title,
            "kind": self.kind, "subtitle": self.subtitle,
            "summary": self.summary, "metrics": self.metrics, "wide": self.wide,
            "series": [s.to_dict() for s in self.series],
            "defaults": self.defaults, "view": self.view.to_dict(),
        }


@dataclass
class Tile:
    """One number. The guide is explicit that this is not a one-bar bar chart."""

    label: str
    value: str
    unit: str = ""
    source: str = ""
    delta: str = ""
    delta_kind: str = ""     # "good" | "bad" | "" -- the UI colors from this

    def to_dict(self) -> dict[str, str]:
        return {"label": self.label, "value": self.value, "unit": self.unit,
                "source": self.source, "delta": self.delta,
                "delta_kind": self.delta_kind}


class Canvas:
    """A figure that saves itself into the report and records the manifest entry.

    `index` is passed explicitly by the catalogue so numbering is stable across
    re-runs: a run with no `map.log` leaves the 09/10 gap rather than sliding
    every later chart up a number and invalidating a saved note (§III.5).
    """

    def __init__(self, out_dir: Path) -> None:
        self.out_dir = out_dir
        self.charts: list[Chart] = []

    #: Inches. Past this a figure is a full-width card in the gallery.
    WIDE_INCHES = 10.0

    def save(self, fig: plt.Figure, chart: Chart, index: int | None = None) -> Chart:
        chart.file = f"{index if index is not None else len(self.charts) + 1:02d}_{chart.id}.png"
        # `bool(...)` is not decoration: `get_figwidth` hands back a numpy
        # float, so the comparison yields `numpy.bool_` and the manifest fails
        # to serialise on the way out of /reports/analyze.
        chart.wide = bool(fig.get_figwidth() >= self.WIDE_INCHES)
        fig.savefig(self.out_dir / chart.file)
        plt.close(fig)
        self.charts.append(chart)
        return chart


# ------------------------------------------------------------------- helpers
def fmt(value: float, digits: int | None = None) -> str:
    """Three useful digits, without scientific notation for ordinary numbers."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "—"
    if digits is not None:
        return f"{value:,.{digits}f}"
    if value == 0:
        return "0"
    magnitude = abs(value)
    if magnitude >= 1e6 or magnitude < 1e-3:
        return f"{value:.2e}"
    if magnitude >= 100:
        return f"{value:,.0f}"
    if magnitude >= 10:
        return f"{value:.1f}".removesuffix(".0")
    return f"{value:.2f}".rstrip("0").rstrip(".")


def tidy(ax: plt.Axes, *, categorical: str = "", hide: Sequence[str] = ("top", "right")) -> None:
    """Hide spines and drop the grid on the categorical axis (§4)."""
    for side in hide:
        ax.spines[side].set_visible(False)
    ax.set_axisbelow(True)
    if categorical:
        ax.grid(axis=categorical, visible=False)


def headroom(ax: plt.Axes, values, factor: float = 1.18) -> None:
    """`ylim(0, max*factor)` so value labels clear the top spine (§5)."""
    finite = [float(v) for v in np.ravel(values) if math.isfinite(float(v))]
    if not finite:
        return
    top, bottom = max(finite), min(finite)
    if bottom >= 0:
        ax.set_ylim(0, (top if top > 0 else 1) * factor)
    else:
        span = (top - bottom) or abs(top) or 1.0
        ax.set_ylim(bottom - span * (factor - 1), top + span * (factor - 1))


def label_bars(ax: plt.Axes, bars, values, fmt_="{:.2f}", fontsize=8.5, color=INK_2) -> None:
    """Direct value labels above bars — the relief for sub-3:1 fills (§2).

    NaN values draw no bar, so they get no label: an empty group is a reading
    the run did not produce, and a `0` there would be a number nobody measured.
    """
    ax.bar_label(
        bars, padding=3, fontsize=fontsize, color=color,
        labels=[fmt_.format(v) if math.isfinite(float(v)) else "" for v in values],
    )


def takeaway(ax: plt.Axes, text: str, y: float = -0.17) -> None:
    """One muted line under the plot saying what it shows — not a second title."""
    ax.annotate(text, xy=(0.5, y), xycoords="axes fraction", ha="center",
                va="top", fontsize=9.5, color=MUTED)


def suptitle(fig: plt.Figure, text: str, y: float = 1.02) -> None:
    """`y=1.02` plus `tight_layout()`, or it overlaps the panel titles (§V.2).

    Pass a larger `y` when `panel_legend` is going above the panels too.
    """
    fig.suptitle(text, fontsize=13.5, fontweight="semibold", color=INK, y=y)
    fig.tight_layout()


def smooth_window(count: int) -> int:
    """Rolling-mean width, or 0 when the series is short enough to read raw."""
    return 0 if count < 24 else max(5, min(31, count // 10 | 1))


def stable_smooth(count: int, full: int) -> int:
    """Smoothing width taken from the whole run, not from the slice on screen.

    Two analysis windows of one run overlap, and where they overlap they hold
    the same readings -- so they have to draw the same line. Sizing the rolling
    mean by `len(the window's readings)` breaks that: 5-75% of a run keeps 354
    readings and smooths over 31, 25-75% keeps 256 and smooths over 25, and the
    two curves then disagree at the shared right-hand end where the underlying
    readings are identical. The endpoint label disagrees with them.

    So the width is chosen once from the unwindowed series and reused. A window
    too short to carry that width falls back to its own -- an over-wide centred
    mean flattens a short series to a horizontal line, which is worse than the
    two curves differing at a size where there is nothing to compare anyway.
    """
    width = smooth_window(full or count)
    return width if count >= width else smooth_window(count)


def rolling_mean(values: np.ndarray, window: int) -> np.ndarray:
    """Centred rolling mean that keeps the array length (edges use what exists)."""
    out = np.empty(len(values), dtype=float)
    half = window // 2
    for i in range(len(values)):
        lo, hi = max(0, i - half), min(len(values), i + half + 1)
        out[i] = values[lo:hi].mean()
    return out


def entity_colors(names: Sequence[str]) -> dict[str, str]:
    """`{entity: color}` in slot order — never `palette[i]` over a filtered list.

    Color follows the entity, so filtering a series out cannot repaint the
    survivors (§1). Raises past the 8th slot rather than truncating: a caller
    with more entities than slots has to fold the tail into "Other" or facet,
    and silently handing back a short dict would only move the failure.
    """
    return dict(zip(names, pal.series_colors(len(names))))


def applied(chart: Chart, view: View, *, title: str, xlabel: str = "",
            ylabel: str = "", shown: Shown | None = None) -> Chart:
    """Record what a chart offered and what the view did with it.

    `chart.title` becomes the *effective* title, so the card header in the UI
    always says the same thing as the image below it.
    """
    chart.title = view.titled(title)
    chart.defaults = {"title": title, "xlabel": xlabel, "ylabel": ylabel}
    chart.view = view
    chart.series = list(shown.all) if shown else []
    return chart


def offsets(count: int, width: float, gap: float = 0.03) -> np.ndarray:
    """Grouped-bar offsets: `(i - (n-1)/2) * (width + gap)`."""
    return (np.arange(count) - (count - 1) / 2) * (width + gap)


def grouped_x(rows: Sequence[Sequence[float]], width: float,
              gap: float = 0.03) -> list[np.ndarray]:
    """Bar positions for `rows[series][category]`, one array per series.

    A category where only one series has a reading gets that bar **centred on
    its tick** rather than parked in its slot beside an empty one. The offset
    exists to separate bars from each other; with nothing to separate from, it
    reads as a missing bar rather than a measure that does not apply.
    """
    count = len(rows)
    off = offsets(count, width, gap)
    x = np.arange(len(rows[0]), dtype=float) if count else np.array([])
    out = [x + off[i] for i in range(count)]
    for j in range(len(x)):
        live = [i for i in range(count) if math.isfinite(float(rows[i][j]))]
        if len(live) == 1:
            out[live[0]][j] = x[j]
    return out


def band(ax: plt.Axes, values, pad: float = 0.12, top_pad: float | None = None) -> None:
    """`ylim` around the data, **not** anchored at zero.

    Right for a box or a spread, where position carries the meaning and a zero
    baseline squashes the whole distribution into a sliver. Wrong for bars,
    whose length is the encoding — those keep `headroom`.
    """
    finite = [float(v) for v in np.ravel(values) if math.isfinite(float(v))]
    if not finite:
        return
    lo, hi = min(finite), max(finite)
    span = (hi - lo) or abs(hi) or 1.0
    ax.set_ylim(lo - span * pad, hi + span * (top_pad if top_pad is not None else pad))


def panel_legend(fig: plt.Figure, ax: plt.Axes, ncol: int, y: float = 1.015) -> None:
    """One legend for a multi-panel figure, centred under the suptitle.

    Every panel shows the same series, so a legend inside any one of them is
    both repeated information and the thing most likely to land on a mark.
    Above the panels it cannot collide with anything (§5).
    """
    handles, labels = ax.get_legend_handles_labels()
    if not handles:
        return
    fig.legend(handles, labels, loc="upper center", bbox_to_anchor=(0.5, y),
               ncol=ncol, frameon=False, fontsize=9.5, labelcolor=INK_2)
