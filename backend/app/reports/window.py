"""Analyse only part of a run: the batches between the a% and b% marks.

A run's interesting stretch is rarely the whole file. The first batches are
warm-up -- models loading, queues filling, the first frames going out before
anything has settled -- and the last ones are the tail draining as the source
runs out. Charted with everything else they drag the mean around and put a
ramp on both ends of every timeline, which is the shape of the *harness*, not
of the split.

So the operator can fix a static window before analysing: `5` to `90` keeps
batches 5 through 90 of a hundred, and nothing outside them is read.

**Static** is the whole design. The bounds are chosen once, applied to every
series in the report, and stored with it -- so a window is a property of the
report the same way the source directory is, and re-drawing a chart months
later produces the same figure. There is no auto-detected warm-up here on
purpose: a threshold that moves by itself would make two reports of the same
run disagree with no record of why.

What a window can and cannot narrow is the other half of the contract. The
per-batch series -- `batch_done_ns.log`, `fps_cluster_ns.log`, `map_window.log`
-- are sequences of readings, so a slice of them is a real measurement of that
stretch. The summary files are not: `fps_cluster.log` holds one line per
cluster that the *run* computed over its own full duration, and no arithmetic
here can turn that into the throughput of batches 5-90. Those charts keep
saying "whole run", out loud, rather than being quietly re-labelled (see
`runcharts.render`).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence, TypeVar

T = TypeVar("T")

#: The whole run, which is what every report was before this existed.
FULL_START = 0.0
FULL_END = 100.0

#: Percentages, so two decimals is already finer than any run resolves.
_ROUND = 2
#: Guards `ceil()` against 5% of 100 arriving as 5.000000000000001.
_EPSILON = 1e-9


@dataclass(frozen=True)
class Window:
    """The `start`-`end` percent slice of a run to analyse. Both inclusive.

    Positions are counted over the readings a series holds, not over the clock:
    reading *k* of *n* sits at `100 * k / n` percent, and is kept when that
    falls inside the band. With 100 batches, `Window(5, 90)` keeps batches 5
    through 90 -- which is what an operator who typed 5 and 90 asked for.

    Counting over readings rather than seconds is deliberate. A stalled cluster
    emits fewer batches per second, so a wall-clock slice would take a
    different number of batches from each of them; the percentages then mean
    something different per series, which is exactly the drift a *static*
    window exists to avoid.
    """

    start: float = FULL_START
    end: float = FULL_END

    # ------------------------------------------------------------- building
    @classmethod
    def of(cls, start: float | None, end: float | None) -> "Window":
        """Clamp a pair of numbers into a usable window.

        The API validates its input, so this is the second line: a window that
        arrived inverted or out of range from an older manifest becomes the
        whole run rather than an empty report.
        """
        try:
            low = round(float(FULL_START if start is None else start), _ROUND)
            high = round(float(FULL_END if end is None else end), _ROUND)
        except (TypeError, ValueError):
            return cls()
        if not (math.isfinite(low) and math.isfinite(high)):
            return cls()
        low = min(max(low, FULL_START), FULL_END)
        high = min(max(high, FULL_START), FULL_END)
        return cls(low, high) if low < high else cls()

    @classmethod
    def from_dict(cls, data: Any) -> "Window":
        if not isinstance(data, dict):
            return cls()
        return cls.of(data.get("start"), data.get("end"))

    def to_dict(self) -> dict[str, Any]:
        return {"start": self.start, "end": self.end, "label": self.label}

    # ------------------------------------------------------------- reading
    @property
    def whole(self) -> bool:
        """Is this the entire run? The unwindowed path stays the default one."""
        return self.start <= FULL_START and self.end >= FULL_END

    @property
    def label(self) -> str:
        """`5–90%` — the en dash, because it is a range and not a subtraction."""
        if self.whole:
            return "whole run"
        return f"{_trim(self.start)}–{_trim(self.end)}%"

    def bounds(self, count: int) -> tuple[int, int]:
        """`(first, stop)` indices into a series of `count` readings.

        Reading *k* (1-based) is kept when `start <= 100k/count <= end`, so the
        pair is `ceil(count*start/100) - 1` and `floor(count*end/100)`.

        A window narrow enough to select nothing still yields one reading. An
        empty series is a chart that silently disappears, and "your window was
        too tight for this run" is not something a blank gallery can say.
        """
        if count <= 0:
            return 0, 0
        if self.whole:
            return 0, count
        first = math.ceil(count * self.start / 100.0 - _EPSILON) - 1
        stop = math.floor(count * self.end / 100.0 + _EPSILON)
        first = min(max(first, 0), count - 1)
        stop = min(max(stop, first + 1), count)
        return first, stop

    def clip(self, rows: Sequence[T]) -> list[T]:
        """The readings inside the window, in the order they were given."""
        if self.whole:
            return list(rows)
        first, stop = self.bounds(len(rows))
        return list(rows[first:stop])


def _trim(value: float) -> str:
    """`5.0` -> `5`, `12.5` -> `12.5`. A percent box should read like one."""
    return f"{value:.2f}".rstrip("0").rstrip(".") or "0"
