"""The chart palette from `guides/visual_guide.md`, plus the validator it demands.

Section 3 of the guide is explicit that the palette is checked by *computing*
it, never by eye, and that the reference JS validator is unusable here because
`node` is not installed -- so the checks are ported to Python and pinned by
`tests/test_reports.py` against the guide's own sanity numbers.

Nothing in here imports matplotlib: the palette is data plus arithmetic, and
`charts.py` is the only module that needs a plotting backend.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from itertools import combinations
from typing import Iterable, Literal, Sequence

Theme = Literal["light", "dark"]

# --------------------------------------------------------------- the palette
#: Categorical slots, guide §2. **Order is the colorblind-safety mechanism**:
#: take slot 1, then 2, then 3 -- never cycle, never generate a 9th hue.
SLOTS_LIGHT: tuple[str, ...] = (
    "#2a78d6",  # 1 blue
    "#eb6834",  # 2 orange
    "#1baf7a",  # 3 aqua
    "#eda100",  # 4 yellow
    "#e87ba4",  # 5 magenta
    "#008300",  # 6 green
    "#4a3aa7",  # 7 violet
    "#e34948",  # 8 red
)
SLOTS_DARK: tuple[str, ...] = (
    "#3987e5", "#d95926", "#199e70", "#c98500",
    "#d55181", "#008300", "#9085e9", "#e66767",
)
SLOT_HUES: tuple[str, ...] = (
    "blue", "orange", "aqua", "yellow", "magenta", "green", "violet", "red",
)

#: Chrome & ink, guide §2. Charts render on the light surface: its contrast
#: floors are the ones the guide's sanity numbers were computed against, and a
#: PNG keeps whatever theme it was drawn in long after it is saved.
CHROME: dict[Theme, dict[str, str]] = {
    "light": {
        "surface": "#fcfcfb", "ink": "#0b0b0b", "ink_2": "#52514e",
        "muted": "#898781", "grid": "#e1e0d9", "axis": "#c3c2b7",
    },
    "dark": {
        "surface": "#1a1a19", "ink": "#ffffff", "ink_2": "#c3c2b7",
        "muted": "#898781", "grid": "#2c2c2a", "axis": "#383835",
    },
}

#: Sequential ramp, light->dark (guide §2). One hue, never a rainbow.
SEQUENTIAL: tuple[str, ...] = ("#cde2fb", "#86b6ef", "#3987e5", "#256abf", "#104281")

#: Diverging: two hues either side of a neutral gray midpoint.
DIVERGING = ("#2a78d6", "#f0efec", "#e34948")

#: Status colors. Never themed, never reused as a series color, and always
#: shipped with a text label -- `charts.py` spells the verdict out in words.
STATUS: dict[str, str] = {
    "good": "#0ca30c", "warning": "#fab219", "serious": "#ec835a", "critical": "#d03b3b",
}

#: Guide §3 thresholds.
L_BAND: dict[Theme, tuple[float, float]] = {"light": (0.43, 0.77), "dark": (0.48, 0.67)}
CHROMA_FLOOR = 0.10
CVD_TARGET = 8.0        # ΔE×100; 6-8 is legal only with a secondary encoding
CVD_MINIMUM = 6.0
NORMAL_FLOOR = 15.0     # hard gate -- secondary encoding does not excuse it
CONTRAST_FLOOR = 3.0    # WCAG vs the chart surface, else the relief rule


# ----------------------------------------------------------------- color math
def hex_to_srgb(value: str) -> tuple[float, float, float]:
    h = value.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        raise ValueError(f"not a hex color: {value!r}")
    return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]


def _to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _to_gamma(c: float) -> float:
    c = min(1.0, max(0.0, c))
    return c * 12.92 if c <= 0.0031308 else 1.055 * (c ** (1 / 2.4)) - 0.055


def linear_rgb(value: str) -> tuple[float, float, float]:
    r, g, b = hex_to_srgb(value)
    return _to_linear(r), _to_linear(g), _to_linear(b)


def oklab(value: str) -> tuple[float, float, float]:
    """sRGB hex -> OKLab (Ottosson). The space every ΔE below is measured in."""
    r, g, b = linear_rgb(value)
    l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l_, m_, s_ = math.cbrt(l), math.cbrt(m), math.cbrt(s)
    return (
        0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
        1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
        0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_,
    )


def oklch(value: str) -> tuple[float, float, float]:
    """(L, C, H°) -- the lightness band and chroma floor are read off this."""
    L, a, b = oklab(value)
    return L, math.hypot(a, b), math.degrees(math.atan2(b, a)) % 360.0


def delta_e(one: str, two: str) -> float:
    """Euclidean OKLab distance ×100, which is the unit the guide's floors use."""
    a, b = oklab(one), oklab(two)
    return 100.0 * math.dist(a, b)


def relative_luminance(value: str) -> float:
    r, g, b = linear_rgb(value)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(one: str, two: str) -> float:
    a, b = relative_luminance(one), relative_luminance(two)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


# ----------------------------------------------------- CVD simulation (Machado)
#: Machado, Oliveira & Fernandes (2009), severity 1.0. Applied in **linear**
#: RGB -- the matrices are defined against linear light, and running them on
#: gamma-encoded values quietly inflates every separation.
MACHADO: dict[str, tuple[tuple[float, float, float], ...]] = {
    "protan": (
        (0.152286, 1.052583, -0.204868),
        (0.114503, 0.786281, 0.099216),
        (-0.003882, -0.048116, 1.051998),
    ),
    "deutan": (
        (0.367322, 0.860646, -0.227968),
        (0.280085, 0.672501, 0.047413),
        (-0.011820, 0.042940, 0.968881),
    ),
}


def simulate_cvd(value: str, kind: str) -> str:
    """Hex -> hex as seen with protanopia/deuteranopia at full severity."""
    m = MACHADO[kind]
    r, g, b = linear_rgb(value)
    out = [row[0] * r + row[1] * g + row[2] * b for row in m]
    return "#" + "".join(f"{round(_to_gamma(c) * 255):02x}" for c in out)


def cvd_delta_e(one: str, two: str) -> float:
    """Worst separation of a pair across both simulated deficiencies."""
    return min(
        delta_e(simulate_cvd(one, kind), simulate_cvd(two, kind))
        for kind in MACHADO
    )


# ------------------------------------------------------------------ validator
@dataclass
class SlotCheck:
    index: int
    hex: str
    hue: str
    lightness: float
    chroma: float
    contrast: float
    in_band: bool
    chroma_ok: bool
    contrast_ok: bool

    @property
    def needs_relief(self) -> bool:
        """Sub-3:1 against the surface: ship direct labels or a table (§2)."""
        return not self.contrast_ok


@dataclass
class PairCheck:
    a: int
    b: int
    cvd: float
    normal: float

    @property
    def ok(self) -> bool:
        return self.normal >= NORMAL_FLOOR and self.cvd >= CVD_MINIMUM

    @property
    def needs_secondary_encoding(self) -> bool:
        return CVD_MINIMUM <= self.cvd < CVD_TARGET


@dataclass
class PaletteReport:
    theme: Theme
    surface: str
    pairing: str
    slots: list[SlotCheck] = field(default_factory=list)
    pairs: list[PairCheck] = field(default_factory=list)

    @property
    def worst_cvd(self) -> float:
        return min((p.cvd for p in self.pairs), default=float("inf"))

    @property
    def worst_normal(self) -> float:
        return min((p.normal for p in self.pairs), default=float("inf"))

    @property
    def relief_required(self) -> list[int]:
        return [s.index for s in self.slots if s.needs_relief]

    @property
    def ok(self) -> bool:
        return (
            all(s.in_band and s.chroma_ok for s in self.slots)
            and all(p.ok for p in self.pairs)
        )

    def failures(self) -> list[str]:
        """Human-readable reasons, empty when the palette passes."""
        out: list[str] = []
        for s in self.slots:
            if not s.in_band:
                lo, hi = L_BAND[self.theme]
                out.append(f"slot {s.index} ({s.hue}) L={s.lightness:.3f} outside {lo}-{hi}")
            if not s.chroma_ok:
                out.append(f"slot {s.index} ({s.hue}) C={s.chroma:.3f} below {CHROMA_FLOOR}")
        for p in self.pairs:
            if p.normal < NORMAL_FLOOR:
                out.append(f"slots {p.a}+{p.b} normal ΔE={p.normal:.1f} below {NORMAL_FLOOR}")
            elif p.cvd < CVD_MINIMUM:
                out.append(f"slots {p.a}+{p.b} CVD ΔE={p.cvd:.1f} below {CVD_MINIMUM}")
        return out


def pairlist(count: int, pairing: str) -> list[tuple[int, int]]:
    """Which pairs can end up side by side.

    Bars, lines and stacks only ever abut their neighbour, so adjacent pairs
    are the honest list. Scatter, bubble, choropleth and small multiples can
    put *any* two series together, so every pair has to clear the floors --
    which is why those forms cap at three slots (§2).
    """
    if pairing == "all":
        return list(combinations(range(1, count + 1), 2))
    return [(i, i + 1) for i in range(1, count)]


def validate(
    slots: Sequence[str] | None = None,
    *,
    theme: Theme = "light",
    surface: str | None = None,
    pairing: str = "adjacent",
    hues: Iterable[str] = SLOT_HUES,
) -> PaletteReport:
    """Run the guide's §3 checks over a slot list.

    >>> validate().worst_cvd >= CVD_TARGET
    True
    """
    colors = list(slots if slots is not None else (SLOTS_LIGHT if theme == "light" else SLOTS_DARK))
    bg = surface or CHROME[theme]["surface"]
    lo, hi = L_BAND[theme]
    hue_names = list(hues)

    report = PaletteReport(theme=theme, surface=bg, pairing=pairing)
    for i, color in enumerate(colors, start=1):
        L, C, _ = oklch(color)
        ratio = contrast_ratio(color, bg)
        report.slots.append(
            SlotCheck(
                index=i,
                hex=color,
                hue=hue_names[i - 1] if i <= len(hue_names) else f"slot {i}",
                lightness=L,
                chroma=C,
                contrast=ratio,
                in_band=lo <= L <= hi,
                chroma_ok=C >= CHROMA_FLOOR,
                contrast_ok=ratio >= CONTRAST_FLOOR,
            )
        )

    for a, b in pairlist(len(colors), pairing):
        report.pairs.append(
            PairCheck(
                a=a, b=b,
                cvd=cvd_delta_e(colors[a - 1], colors[b - 1]),
                normal=delta_e(colors[a - 1], colors[b - 1]),
            )
        )
    return report


def series_colors(count: int, *, theme: Theme = "light", all_pairs: bool = False) -> list[str]:
    """Slot colors for `count` series, in slot order.

    Raises rather than cycling: a 9th generated hue is exactly the failure the
    ordering exists to prevent. Callers past the cap fold into "Other" or facet
    into small multiples (§1).
    """
    table = SLOTS_LIGHT if theme == "light" else SLOTS_DARK
    cap = 3 if all_pairs else len(table)
    if count > cap:
        raise ValueError(
            f"{count} series exceeds the {cap}-slot cap for "
            f"{'all-pairs' if all_pairs else 'adjacent'} forms; fold into 'Other' or facet"
        )
    return list(table[:count])
