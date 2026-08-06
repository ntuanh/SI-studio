"""Analyse a fixed slice of a run: from a% to b% of the system's own clock.

A run's interesting stretch is rarely the whole file. The first batches are
warm-up -- models loading, queues filling, the first frames going out before
anything has settled -- and the last ones are the tail draining as the source
runs out. Charted with everything else they drag the mean around and put a ramp
on both ends of every timeline, which is the shape of the *harness*, not of the
split.

So the operator fixes a static window before analysing: `5` to `95` throws away
the first 5% and the last 5% of the run and reports the rest.

**One clock for the whole report.** The window is a span of wall-clock time
taken from the system's own run -- first batch completed to last batch
completed -- and every series, every event and every recomputed figure is cut
to that same span. It is deliberately *not* a per-series slice: cutting each
cluster at its own 5% mark would give the clusters different spans, and then
"cluster 0 against cluster 1" compares two different stretches of the run,
which is the one thing a comparison must never do.

**Recomputed, not relabelled.** Where the run wrote the raw per-batch events,
the summary figures are computed again from the events inside the span, by the
same recipe the run used -- see `runlog.recompute`. Throughput is the clearest
case: 504 batches over 1340 s is 12.0 FPS, and the same arithmetic over the
454 batches inside a 5-95% span is the throughput of that span.

**Static** is the rest of the design. The bounds are chosen once, applied to
every series in the report, and stored with it -- so a window is a property of
the report the way the source directory is, and re-drawing a chart months later
produces the same figure. There is no auto-detected warm-up on purpose: a
threshold that moved by itself would make two reports of the same run disagree
with no record of why.
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
    """The `start`-`end` percent slice of a run to analyse. Both ends inclusive.

    The percentages are of the run's **elapsed time**, measured on the system
    clock the result files already share: `Window(5, 95)` over a 1340-second
    run is the 1206 seconds between 67 s and 1273 s in, and everything the run
    recorded with a timestamp is judged against those two moments.
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
        """`5–95%` — the en dash, because it is a range and not a subtraction."""
        if self.whole:
            return "whole run"
        return f"{_trim(self.start)}–{_trim(self.end)}%"

    # ------------------------------------------------------- the time span
    def span(self, first: float, last: float) -> tuple[float, float]:
        """`(from, to)` on the run's own clock, given when it started and ended.

        This is the one place the percentages become moments. Every caller cuts
        against the pair this returns, which is what keeps a cluster series, a
        split-point marker and a recomputed throughput describing the same
        stretch of the same run.
        """
        length = max(last - first, 0.0)
        return first + length * self.start / 100.0, first + length * self.end / 100.0

    def holds(self, at: float, span: tuple[float, float]) -> bool:
        """Is a moment inside the span? Half-open: `(from, to]`. NaN never is.

        Open at the start on purpose. A batch that completed *at* the opening
        moment did its work before the window, so counting it would put one
        batch's worth of frames into a stretch that did not produce them --
        and a rate over the span is exactly what gets recomputed here.

        This is not a technicality invented for the edge case: it is the run's
        own convention. `steady_fps` is `(done - 1) * batch_size / elapsed` --
        every completion after the first, over the time since the first -- so a
        window covering the whole clock reproduces it exactly.
        """
        return bool(math.isfinite(at) and span[0] < at <= span[1])

    # ------------------------------- readings, for logs with no usable clock
    def bounds(self, count: int) -> tuple[int, int]:
        """`(first, stop)` indices into a series of `count` readings.

        The fallback for the schema-blind path, where files share no clock and
        a reading's position in the file is the only ordering there is.
        Reading *k* (1-based) is kept when `start <= 100k/count <= end`.

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
