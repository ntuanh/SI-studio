#!/usr/bin/env python3
"""Chart the dynamic-network schedule the way compare_runs.ipynb charts the
static one, and write PNGs only.

Same parsers, same palette, same marks as `imgs/total`. Two things are
deliberately different:

* **Axis limits are read off the data, not hardcoded.** The notebook's `38 fps`
  / `80 s` / `880 MB` were sized for the static runs. Reused unchanged under a
  shaped network they would clip the very slowdown the runs exist to show.
* **The undercount correction is derived, not tabled.** `KNOWN_UNDERCOUNT`
  named PA and a batch count by hand; here a run's own `batch_done_ns.log` is
  the second counter, so a disagreement is found rather than remembered.

    python make_dynamic_imgs.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import matplotlib as mpl
import numpy as np
import pandas as pd

mpl.use("Agg")  # no display on this box, and nothing here is interactive
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import PathPatch, Rectangle  # noqa: E402
from matplotlib.path import Path as MplPath  # noqa: E402

HERE = Path(__file__).resolve().parent
ROOT = HERE / "results dynamic network"
#: `python make_dynamic_imgs.py <name>` writes to imgs/<name>, so a new run gets
#: a new folder instead of overwriting the set someone is already reading.
OUTDIR = HERE / "imgs" / (sys.argv[1] if len(sys.argv) > 1 else "dynamic")

#: display name -> folder, note. Order here is the order used everywhere.
#: The names follow controller/dirs.txt: `results_dmsf` is dmsf and
#: `results_standalone` is standalone (compare_runs.ipynb has these two
#: crossed for the static set -- not reproduced here).
SYSTEMS = [
    ("Split",      "results_split",      "proposed, adaptive cut off"),
    ("Dynamic",    "results_dynamic",    "same code as Split, adaptive cut on"),
    ("PA",         "results_PA",         "privacy-aware, 9 clusters"),
    ("DMSF",       "results_dmsf",       "per-batch routing, auto split point"),
    ("Standalone", "results_standalone", "baseline, standalone inference"),
    ("Dynamic-tuned", "results_dynamic_tuned",
     "adaptive retuned: shallower disabled, deeper biased"),
]

BATCH = 32  # frames per batch, for the corrected-FPS arithmetic

# ------------------------------------------------------------------- parsing
NUM = r"([-+]?\d*\.?\d+)"


def kv(line, key, cast=float):
    """Value of `key=` on a log line, or None."""
    m = re.search(rf"\b{re.escape(key)}=" + NUM, line)
    return cast(m.group(1)) if m else None


def lines(path):
    p = Path(path)
    return p.read_text(errors="replace").splitlines() if p.exists() else []


def token(line, i=1):
    parts = line.split()
    return parts[i] if len(parts) > i else ""


def load_run(folder):
    """Every metric this script plots, for one results folder."""
    d = ROOT / folder
    r = {}

    for L in lines(d / "fps_cluster.log"):
        if " SYSTEM " in L:
            r["fps"] = kv(L, "fps")
            r["done"] = kv(L, "done", int)
            r["frames"] = kv(L, "frames", int)

    for L in lines(d / "latency_cluster.log"):
        if "SYSTEM" in L and "kind=e2e" in L:
            for k in ("mean_ms", "p50_ms", "p95_ms", "max_ms"):
                r[k] = kv(L, k)

    for L in lines(d / "utilization_cluster.log"):
        if token(L) == "SYSTEM":
            r["util"] = kv(L, "utilization")
            r["busy_s"] = kv(L, "busy_s")
            r["span_s"] = kv(L, "total_s")

    ft = lines(d / "free_time_cluster.log")
    for L in ft:
        if token(L) == "SYSTEM" and " free=" in L and " FREE " not in L:
            r["free"] = kv(L, "free")
            r["free_s"] = kv(L, "free_s")

    # some runs wrote no SYSTEM rollup for FREE/KIND -> fold the cluster lines
    has_sys = any(token(L) == "SYSTEM" and " FREE " in L for L in ft)
    reasons, kinds = {}, {}
    for L in ft:
        use = (token(L) == "SYSTEM") if has_sys else token(L).startswith("cluster=")
        if not use:
            continue
        m = re.search(r"FREE reason=(\S+)", L)
        if m:
            reasons[m.group(1)] = reasons.get(m.group(1), 0.0) + (kv(L, "free_s") or 0.0)
        m = re.search(r"KIND kind=(\S+)", L)
        if m:
            kinds[m.group(1)] = kinds.get(m.group(1), 0.0) + (kv(L, "busy_s") or 0.0)
    r["free_reasons"], r["kinds"] = reasons, kinds

    cl_span, cl_kinds = {}, {}
    for L in ft:
        m = re.search(r"cluster=(\S+)", L)
        if not m:
            continue
        if " ALL " in L:
            cl_span[m.group(1)] = kv(L, "span_s")
        k = re.search(r"KIND kind=(\S+)", L)
        if k:
            cl_kinds.setdefault(m.group(1), {})[k.group(1)] = kv(L, "busy_s") or 0.0
    cl_kinds = {c: k for c, k in cl_kinds.items() if cl_span.get(c)}
    r["cluster_spans"], r["cluster_kinds"] = cl_span, cl_kinds

    for L in lines(d / "message_size.log"):
        r["mb_per_frame"] = kv(L, "per_frame_mb")
        r["mb_per_msg"] = kv(L, "mean_mb")
        m = re.search(r"compress=(\S+)", L)
        r["compress"] = m.group(1) if m else None

    txt = "\n".join(lines(d / "broker_ram.log"))
    if txt and "disabled" not in txt:
        r["broker_over_idle"] = kv(txt, "run_minus_idle_mb")
        r["broker_peak_over_idle"] = kv(txt, "run_peak_over_idle_mb")
    else:
        r["broker_over_idle"] = np.nan
        r["broker_peak_over_idle"] = np.nan

    ts = sorted(int(L.split()[0]) for L in lines(d / "batch_done_ns.log") if L.strip())
    if ts:
        r["wall_s"] = (ts[-1] - ts[0]) / 1e9
        r["done_events"] = len(ts)

    return r


# ---------------------------------------------------------------- the table
def build() -> tuple[pd.DataFrame, dict]:
    present = [e for e in SYSTEMS if (ROOT / e[1]).is_dir()]
    for name, folder, _ in SYSTEMS:
        if (name, folder) not in [(n, f) for n, f, _ in present]:
            print(f"skipping {name}: no {folder}/ under {ROOT.name}")
    if not present:
        raise FileNotFoundError(f"no SYSTEMS folder found under {ROOT}")
    # Dropped, never carried: an entry with no folder would load as all-nan and
    # reach the figures as a blank row rather than as an error.
    SYSTEMS[:] = present
    runs = {name: load_run(folder) for name, folder, _ in SYSTEMS}

    rows = []
    for name, folder, note in SYSTEMS:
        r = runs[name]
        # The run carries its own second counter: one line per completed batch
        # in batch_done_ns.log. Where the two disagree by more than a rounding
        # error, the completion stamps are the ones that were written per event.
        done, events = r.get("done"), r.get("done_events")
        real = events if (done and events and abs(events - done) > max(2, 0.01 * done)) else None
        rows.append({
            "system": name,
            "note": note,
            "folder": folder,
            "fps_reported": r.get("fps"),
            "fps_corrected": (real * BATCH / r["wall_s"]) if real and r.get("wall_s") else r.get("fps"),
            "p50_s": r.get("p50_ms", np.nan) / 1000,
            "mean_s": r.get("mean_ms", np.nan) / 1000,
            "p95_s": r.get("p95_ms", np.nan) / 1000,
            "max_s": r.get("max_ms", np.nan) / 1000,
            "util_pct": r.get("util"),
            "free_pct": r.get("free"),
            "mb_per_frame": r.get("mb_per_frame"),
            "mb_per_msg": r.get("mb_per_msg"),
            "broker_over_idle_mb": r.get("broker_over_idle"),
            "batches": done,
            "batches_real": real or done,
            "wall_s": r.get("wall_s"),
            "span_s": r.get("span_s"),
        })

    df = pd.DataFrame(rows).set_index("system")
    df["counter_ok"] = df["batches"] == df["batches_real"]
    return df, runs


# ------------------------------------------------------------------ palette
# validated: adjacent + all-pairs CVD separation, light surface
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
BASELINE = "#c3c2b7"

S1, S2, S3 = "#2a78d6", "#eb6834", "#1baf7a"   # blue, orange, aqua
DIM = "#b6bcc6"                                 # de-emphasis / "other"
TRACK = "#dfe4ec"

HEAT = ["#f2f4f8", "#e8f0fc", "#cde2fb", "#9ec5f4", "#5598e7", "#256abf", "#184f95"]
HEAT_INK = [MUTED, INK, INK, INK, INK, "#ffffff", "#ffffff"]

mpl.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "font.family": "sans-serif",
    "font.sans-serif": ["Segoe UI", "DejaVu Sans", "Arial"],
    "font.size": 9,
    "text.color": INK, "axes.labelcolor": INK_2, "axes.titlecolor": INK,
    "xtick.color": MUTED, "ytick.color": INK,
    "axes.edgecolor": BASELINE, "axes.linewidth": 0.8,
    "grid.color": GRID, "grid.linewidth": 0.8, "grid.linestyle": "-",  # solid, never dashed
    "axes.grid": False, "axes.axisbelow": True,
    "xtick.major.size": 0, "ytick.major.size": 0,
    "xtick.major.pad": 5, "ytick.major.pad": 5,
    "legend.frameon": False, "legend.fontsize": 8.5,
    "figure.dpi": 110, "savefig.dpi": 200, "savefig.bbox": "tight", "savefig.pad_inches": 0.18,
})

BAR_H = 0.36   # thin marks: the bar never fills its slot
GAP = 0.35     # surface gap between touching segments, in data units


def tidy(ax, xgrid=True, ygrid=False):
    """Hairline grid on one axis, no top/right spines."""
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(BASELINE)
    ax.xaxis.grid(xgrid)
    ax.yaxis.grid(ygrid)
    ax.tick_params(length=0)
    return ax


def _pts_in_data(ax, pts):
    """`pts` typographic points as (dx, dy) in data units."""
    px = pts * ax.figure.dpi / 72.0
    inv = ax.transData.inverted()
    x0, y0 = inv.transform((0, 0))
    x1, y1 = inv.transform((px, px))
    return abs(x1 - x0), abs(y1 - y0)


def rbar(ax, x0, y, w, h, color, round_end=True, zorder=3):
    """Horizontal bar: square at the baseline, 3pt rounded at the data end.

    Call this *after* the axis limits are set -- the corner radius is derived
    from the current data transform.
    """
    if not np.isfinite(w) or w <= 0:
        return
    rx, ry = _pts_in_data(ax, 3.0) if round_end else (0.0, 0.0)
    rx, ry = min(rx, abs(w)), min(ry, h / 2)
    kx, ky = rx * 0.5523, ry * 0.5523
    v = [(x0, y - h/2), (x0 + w - rx, y - h/2),
         (x0 + w - rx + kx, y - h/2), (x0 + w, y - h/2 + ry - ky), (x0 + w, y - h/2 + ry),
         (x0 + w, y + h/2 - ry),
         (x0 + w, y + h/2 - ry + ky), (x0 + w - rx + kx, y + h/2), (x0 + w - rx, y + h/2),
         (x0, y + h/2), (x0, y - h/2)]
    c = [MplPath.MOVETO, MplPath.LINETO,
         MplPath.CURVE4, MplPath.CURVE4, MplPath.CURVE4,
         MplPath.LINETO,
         MplPath.CURVE4, MplPath.CURVE4, MplPath.CURVE4,
         MplPath.LINETO, MplPath.CLOSEPOLY]
    ax.add_patch(PathPatch(MplPath(v, c), facecolor=color, edgecolor="none", zorder=zorder))


# -------------------------------------------------------------------- charts
SER = [("input",        "starved for input",       S1),
       ("backpressure", "blocked by backpressure", S2),
       ("shutdown",     "waiting on shutdown",     S3),
       ("other",        "other / unaccounted",     DIM)]

COLS = [("Inference",  ["inference"]),
        ("Tensor ops", ["tensor"]),
        ("Codec",      ["compress", "decompress", "encode", "decode", "serialize"]),
        ("Transport",  ["send", "recv"]),
        ("Capture",    ["capture", "read_input"]),
        ("Postproc.",  ["postprocess"])]


def headroom(v, pad=1.18, floor=1.0):
    """An axis top that leaves room for the value label past the longest mark."""
    v = float(np.nanmax([v, floor]))
    return v * pad


class Charts:
    """The eight figures, each able to draw into its own axes or a panel."""

    def __init__(self, df, runs):
        self.df, self.runs = df, runs
        self.written = []
        self.order_fps = df.sort_values("fps_reported", ascending=False).index.tolist()
        # Every limit below is read off this run's own numbers. Under a shaped
        # network the spread is not the one the static notebook was drawn for.
        self.fps_max = headroom(df["fps_reported"].max(), 1.22)
        self.lat_max = headroom(df["max_s"].max(), 1.10)
        self.idle_max = headroom(df["free_pct"].max(), 1.30, floor=5)
        self.ram_max = headroom(df["broker_over_idle_mb"].max(), 1.28, floor=50)
        self.wire_lo = df["mb_per_frame"].min() / 2.5
        self.wire_hi = df["mb_per_frame"].max() * 6

    def height(self, base=0.35, pitch=0.52):
        """Figure height that keeps the row pitch fixed as runs are added."""
        return base + pitch * len(self.df)

    def save(self, fig, stem):
        """PNG only -- the deliverable is imgs/dynamic/*.png."""
        p = OUTDIR / f"{stem}.png"
        fig.savefig(p)
        plt.close(fig)
        self.written.append(p)
        print("wrote", p.relative_to(HERE))

    def reason_pct(self, name):
        r = self.runs[name]["free_reasons"]
        span = self.df.loc[name, "span_s"]
        bp = r.get("backpressure", 0) + r.get("broker_backpressure", 0)
        other = sum(r.get(k, 0) for k in ("unaccounted", "downstream", "idle", "stop"))
        return {"input": r.get("input", 0) / span * 100,
                "backpressure": bp / span * 100,
                "shutdown": r.get("shutdown", 0) / span * 100,
                "other": other / span * 100}

    # ---- individual figures ------------------------------------------------
    def throughput(self, ax=None, compact=False):
        fig = None
        if ax is None:
            fig, ax = plt.subplots(figsize=(7.0, self.height()))
        df = self.df
        order = self.order_fps[::-1]
        ax.set_xlim(0, self.fps_max)
        ax.set_ylim(-0.6, len(order) - 0.4)
        ax.set_yticks(range(len(order)))
        ax.set_yticklabels(order)
        ax.set_xlabel("frames per second" if compact else "frames per second (whole fleet)")
        tidy(ax)
        for i, s in enumerate(order):
            row = df.loc[s]
            ok = row["counter_ok"]
            rbar(ax, 0, i, row["fps_reported"], BAR_H, S1 if ok else DIM)
            ax.text(row["fps_reported"] + self.fps_max * 0.018, i,
                    f"{row['fps_reported']:.1f}", va="center", ha="left",
                    color=INK_2, fontsize=8 if compact else 8.5)
            if not ok:
                xc = row["fps_corrected"]
                ax.plot([xc], [i], marker="o", ms=7, mfc=SURFACE, mec=S1, mew=1.8, zorder=4)
                ax.text(xc + self.fps_max * 0.03, i, f"~{xc:.1f} corrected",
                        va="center", ha="left", color=INK_2, fontsize=8.5)
        if fig is not None:
            ax.set_title("Throughput", loc="left", fontsize=11.5,
                         fontweight="semibold", pad=10)
            self.save(fig, "throughput")

    def latency_spread(self, ax=None, compact=False):
        fig = None
        if ax is None:
            fig, ax = plt.subplots(figsize=(7.0, self.height(0.45)))
        df = self.df
        xmax = self.lat_max
        order = df.sort_values("p50_s", ascending=False).index.tolist()
        for i, s in enumerate(order):
            r = df.loc[s]
            a, b = r["p50_s"], r["p95_s"]
            ax.plot([a, b], [i, i], color=TRACK, lw=3.5 if compact else 4,
                    solid_capstyle="round", zorder=2)
            ax.plot([b], [i], marker="o", ms=6 if compact else 7, mfc=SURFACE,
                    mec=S1, mew=1.6 if compact else 1.8, zorder=4)
            ax.plot([a], [i], marker="o", ms=6.5 if compact else 7.5, mfc=S1,
                    mec=SURFACE, mew=1.3 if compact else 1.5, zorder=5)
            if not compact:
                ax.text(a, i + 0.30, f"{a:.0f}", ha="center", va="bottom",
                        color=INK_2, fontsize=8.5)
            over = r["max_s"] > xmax
            mx = xmax * 1.01 if over else r["max_s"]
            ax.plot([mx, mx], [i - 0.22, i + 0.22], color=S2 if over else DIM,
                    lw=1.8, solid_capstyle="round", zorder=4, clip_on=False)
            if over:
                ax.text(mx + xmax * 0.015, i, f"{r['max_s']:.0f} s", va="center",
                        ha="left", color=S2, fontsize=8.5, clip_on=False)
        ax.set_yticks(range(len(order)))
        ax.set_yticklabels(order)
        ax.set_xlim(0, xmax)
        ax.set_ylim(-0.6, len(order) - 0.4)
        ax.set_xlabel("end-to-end latency (s)" if compact
                      else "end-to-end latency per batch (s)")
        tidy(ax)
        h = [plt.Line2D([], [], ls="", marker="o", ms=6.5, mfc=S1, mec=SURFACE, label="p50"),
             plt.Line2D([], [], ls="", marker="o", ms=6.5, mfc=SURFACE, mec=S1,
                        mew=1.7, label="p95"),
             plt.Line2D([], [], color=DIM, lw=1.8, label="worst batch")]
        ax.legend(handles=h, loc="lower right", ncol=3, bbox_to_anchor=(1.0, 1.0),
                  fontsize=7.5 if compact else 8.5)
        if fig is not None:
            ax.set_title("Latency spread", loc="left", fontsize=11.5,
                         fontweight="semibold", pad=10)
            self.save(fig, "latency_spread")

    def wire_cost(self, ax=None, compact=False):
        fig = None
        if ax is None:
            fig, ax = plt.subplots(figsize=(7.0, self.height()))
        df = self.df
        order = df.sort_values("mb_per_frame", ascending=False).index.tolist()
        for i, s in enumerate(order):
            v = df.loc[s, "mb_per_frame"]
            ax.plot([self.wire_lo, v], [i, i], color=TRACK,
                    lw=1.1 if compact else 1.2, zorder=2)
            ax.plot([v], [i], marker="o", ms=7 if compact else 8, mfc=S1,
                    mec=SURFACE, mew=1.3 if compact else 1.5, zorder=4)
            ax.text(v * 1.25, i, f"{v:.4f}" if v < 0.01 else f"{v:.3f}",
                    va="center", ha="left", color=INK_2, fontsize=8 if compact else 8.5)
        ax.set_xscale("log")
        ax.set_xlim(self.wire_lo, self.wire_hi)
        ax.set_yticks(range(len(order)))
        ax.set_yticklabels(order)
        ax.set_ylim(-0.6, len(order) - 0.4)
        ax.set_xlabel("MB per frame (log)" if compact
                      else "MB published per frame by one edge worker (log scale)")
        tidy(ax)
        if fig is not None:
            ax.set_title("Bytes per frame on the wire", loc="left", fontsize=11.5,
                         fontweight="semibold", pad=10)
            self.save(fig, "wire_cost")

    def idle_reasons(self, ax=None, compact=False):
        fig = None
        if ax is None:
            fig, ax = plt.subplots(figsize=(7.0, self.height(0.45)))
        df = self.df
        order = df.sort_values("free_pct", ascending=True).index.tolist()
        ax.set_xlim(0, self.idle_max)
        ax.set_ylim(-0.6, len(order) - 0.4)
        ax.set_yticks(range(len(order)))
        ax.set_yticklabels(order)
        ax.set_xlabel("device wall clock spent idle (%)" if compact
                      else "share of device wall clock spent idle (%)")
        tidy(ax)
        for i, s in enumerate(order):
            parts = self.reason_pct(s)
            live = [(k, lab, c) for k, lab, c in SER if parts[k] > 0.005]
            x = 0.0
            for j, (k, lab, c) in enumerate(live):
                w = parts[k]
                last = j == len(live) - 1
                rbar(ax, x, i, w if last else max(w - GAP, 0.05), BAR_H, c, round_end=last)
                x += w
            label = f"{df.loc[s, 'free_pct']:.0f}%" if compact else f"{df.loc[s, 'free_pct']:.1f}%"
            ax.text(x + self.idle_max * 0.02, i, label, va="center", ha="left",
                    color=INK_2, fontsize=8 if compact else 8.5)
        h = [Rectangle((0, 0), 1, 1, facecolor=c, edgecolor="none", label=lab)
             for _, lab, c in SER]
        # Four long labels in two columns is the widest legend in the set; in a
        # panel it runs back into the title unless it is tightened first.
        ax.legend(handles=h, loc="lower right", ncol=2, bbox_to_anchor=(1.0, 1.0),
                  fontsize=7.0 if compact else 8.5,
                  handlelength=1.1 if compact else 2.0,
                  handletextpad=0.5 if compact else 0.8,
                  columnspacing=1.0 if compact else 2.0)
        if fig is not None:
            ax.set_title("Where the idle time went", loc="left", fontsize=11.5,
                         fontweight="semibold", pad=10)
            self.save(fig, "idle_reasons")

    def tradeoff(self):
        df = self.df
        fig, ax = plt.subplots(figsize=(6.2, 4.2))
        xmax = headroom(df["p50_s"].max(), 1.25)
        for s in df.index:
            r = df.loc[s]
            ax.plot([r["p50_s"]], [r["fps_reported"]], marker="o", ms=8, mfc=S1,
                    mec=SURFACE, mew=1.6, zorder=4)
            ax.annotate(s, (r["p50_s"], r["fps_reported"]), textcoords="offset points",
                        xytext=(0, 11), ha="center", color=INK, fontsize=9.5,
                        fontweight="medium")
            if not r["counter_ok"]:
                ax.plot([r["p50_s"], r["p50_s"]], [r["fps_reported"], r["fps_corrected"]],
                        color=DIM, lw=1.2, zorder=2)
                ax.plot([r["p50_s"]], [r["fps_corrected"]], marker="o", ms=8,
                        mfc=SURFACE, mec=S1, mew=1.8, zorder=4)
        ax.set_xlim(0, xmax)
        ax.set_ylim(0, self.fps_max)
        ax.set_xlabel("median end-to-end latency per batch (s)  →  slower")
        ax.set_ylabel("frames per second  →  faster")
        ax.text(xmax * 0.03, self.fps_max * 0.955, "better ↖", color=MUTED,
                fontsize=9, style="italic", fontfamily="DejaVu Sans")
        tidy(ax, xgrid=False, ygrid=True)
        ax.set_title("Throughput against latency", loc="left", fontsize=11.5,
                     fontweight="semibold", pad=10)
        self.save(fig, "tradeoff")

    def broker_ram(self):
        df = self.df
        fig, ax = plt.subplots(figsize=(7.0, self.height()))
        order = df.sort_values("broker_over_idle_mb", ascending=True,
                               na_position="first").index.tolist()
        ax.set_xlim(0, self.ram_max)
        ax.set_ylim(-0.6, len(order) - 0.4)
        ax.set_yticks(range(len(order)))
        ax.set_yticklabels(order)
        ax.set_xlabel("broker RAM above its own idle baseline (MB)")
        tidy(ax)
        for i, s in enumerate(order):
            v = df.loc[s, "broker_over_idle_mb"]
            if pd.isna(v):
                ax.text(self.ram_max * 0.01, i, "not measured", va="center", ha="left",
                        color=MUTED, fontsize=8.5, style="italic")
                continue
            rbar(ax, 0, i, v, BAR_H, S1)
            ax.text(v + self.ram_max * 0.015, i, f"{v:.0f} MB", va="center",
                    ha="left", color=INK_2, fontsize=8.5)
        ax.set_title("Broker memory above idle", loc="left", fontsize=11.5,
                     fontweight="semibold", pad=10)
        self.save(fig, "broker_ram")

    def activity_heatmap(self):
        df, runs = self.df, self.runs

        def share(kinds, keys, span):
            """Percent of a span spent in any of `keys`."""
            return sum(kinds.get(k, 0) for k in keys) / span * 100

        rows = self.order_fps
        M = np.array([[share(runs[s]["kinds"], keys, df.loc[s, "span_s"])
                       for _, keys in COLS] for s in rows])

        # Spread, measured across the run's own clusters -- the finest unit that
        # carries a KIND breakdown in these folders. A one-cluster run gets nan,
        # not 0: one measurement is not a spread of zero.
        ncl = [len(runs[s]["cluster_kinds"]) for s in rows]
        SD = np.array([[np.std([share(k, keys, runs[s]["cluster_spans"][c])
                                for c, k in runs[s]["cluster_kinds"].items()], ddof=1)
                        if len(runs[s]["cluster_kinds"]) > 1 else np.nan
                        for _, keys in COLS] for s in rows])

        def step(v):
            """Discrete bucket, so every cell ships ink that clears its fill.

            `None` for a value that was never measured. Every test below is `<`,
            which is False for nan, so an unguarded nan would fall through to the
            last bucket and be painted the darkest colour -- absent data reading
            as the maximum.
            """
            if not np.isfinite(v):
                return None
            return (0 if v < 0.1 else 1 if v < 2 else 2 if v < 8 else 3 if v < 20
                    else 4 if v < 45 else 5 if v < 70 else 6)

        fig, ax = plt.subplots(figsize=(7.4, 0.9 + 0.5 * M.shape[0]))
        for i in range(M.shape[0]):
            spread = bool(np.isfinite(SD[i]).any())   # this row has clusters to compare
            for j in range(M.shape[1]):
                k = step(M[i, j])
                if k is None:   # absent is drawn as absent: never zero, never the max
                    ax.add_patch(Rectangle((j + 0.03, i + 0.06), 0.94, 0.88,
                                           facecolor=SURFACE, edgecolor=GRID, linewidth=0.8))
                    ax.text(j + 0.5, i + 0.5, "not measured", ha="center", va="center",
                            color=MUTED, fontsize=7.5, style="italic")
                    continue
                ax.add_patch(Rectangle((j + 0.03, i + 0.06), 0.94, 0.88,
                                       facecolor=HEAT[k], edgecolor="none"))
                dash = M[i, j] < 0.1
                ax.text(j + 0.5, i + (0.40 if spread else 0.5),
                        "–" if dash else
                        (f"{M[i, j]:.0f}" if M[i, j] >= 10 else f"{M[i, j]:.1f}"),
                        ha="center", va="center", color=HEAT_INK[k], fontsize=8.5)
                if spread and not dash and np.isfinite(SD[i, j]):
                    sd = SD[i, j]
                    ax.text(j + 0.5, i + 0.69,
                            f"±{sd:.0f}" if sd >= 10 else f"±{sd:.1f}",
                            ha="center", va="center", color=HEAT_INK[k], fontsize=7.0)

        ax.set_xlim(0, M.shape[1])
        ax.set_ylim(M.shape[0], 0)
        ax.set_xticks(np.arange(M.shape[1]) + 0.5)
        ax.set_xticklabels([c for c, _ in COLS])
        ax.set_yticks(np.arange(M.shape[0]) + 0.5)
        ax.set_yticklabels([f"{s} ·{n}" for s, n in zip(rows, ncl)])
        ax.xaxis.set_ticks_position("top")
        for sp in ax.spines.values():
            sp.set_visible(False)
        ax.tick_params(length=0)
        ax.set_title("Share of device wall clock, by activity (%)", loc="left",
                     fontsize=11.5, fontweight="semibold", pad=44)
        ax.annotate("run total per cell; ±1 s.d. across the run's clusters, whose "
                    "count follows each name (·n)",
                    xy=(0, 1), xycoords="axes fraction", xytext=(0, 28),
                    textcoords="offset points", ha="left", va="bottom",
                    fontsize=8.5, color=INK_2)
        self.save(fig, "activity_heatmap")

    def overview(self):
        fig, axes = plt.subplots(2, 2, figsize=(11.0, 2.0 + 2.0 * self.height(0.45)))
        self.throughput(ax=axes[0, 0], compact=True)
        axes[0, 0].set_title("(a) Throughput", loc="left", fontsize=10.5,
                             fontweight="semibold")
        self.latency_spread(ax=axes[0, 1], compact=True)
        axes[0, 1].set_title("(b) Latency, p50–p95", loc="left", fontsize=10.5,
                             fontweight="semibold")
        self.wire_cost(ax=axes[1, 0], compact=True)
        axes[1, 0].set_title("(c) Wire cost", loc="left", fontsize=10.5,
                             fontweight="semibold")
        self.idle_reasons(ax=axes[1, 1], compact=True)
        # this legend is two rows tall and reaches back over the title line
        axes[1, 1].set_title("(d) Idle time, by cause", loc="left", fontsize=10.5,
                             fontweight="semibold", pad=22)
        fig.tight_layout(pad=1.6, h_pad=2.6, w_pad=3.0)
        self.save(fig, "overview")


def main() -> int:
    OUTDIR.mkdir(parents=True, exist_ok=True)
    df, runs = build()
    print(df[["fps_reported", "p50_s", "p95_s", "util_pct", "free_pct",
              "mb_per_frame", "broker_over_idle_mb", "batches"]].round(3).to_string())
    print()
    c = Charts(df, runs)
    c.throughput()
    c.latency_spread()
    c.tradeoff()
    c.wire_cost()
    c.idle_reasons()
    c.activity_heatmap()
    c.broker_ram()
    c.overview()
    print(f"\n{len(c.written)} PNGs in {OUTDIR.relative_to(HERE)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
