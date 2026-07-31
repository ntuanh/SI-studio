"""Turn a pulled result directory into metric series.

The result directories on the devices are **plain-text logs** -- profiler dumps,
agent stdout, whatever the experiment printed -- so there is no schema to read.
This extracts every `name value unit` it can defend and groups the numbers by
metric, which is what `charts.py` needs to pick a chart form.

The parser is deliberately conservative. A log line is mostly prose, and the
cost of a false positive is a nonsense chart sitting next to real ones, so a
match has to look like a measurement: a name, a separator, a number that is not
part of a date/version/address, and -- for the loosest pattern -- a known unit.

CSV and JSON are handled too. Not because the guide asks for it, but because
result directories almost always end up with a `summary.csv` next to the logs,
and skipping it would drop the one file that already holds the answer.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger(__name__)

#: Extensions worth reading. Everything else in the directory is left alone --
#: a 300 MB `head.pt` has nothing to say and would only slow the pull down.
TEXT_SUFFIXES = {
    ".log", ".txt", ".out", ".err", ".csv", ".tsv", ".json", ".jsonl",
    ".ndjson", ".md", ".yaml", ".yml", ".ini", ".cfg", ".dat", ".stat", ".stats",
}

#: Files with no extension at all (`nohup.out`-style names, `stdout`) are read
#: when they are small enough to be a log rather than a blob.
NO_SUFFIX_MAX_BYTES = 4 * 1024 * 1024

#: Per-file ceiling. A runaway log is truncated rather than skipped: the first
#: N MB of a 2 GB file still describes the run.
MAX_FILE_BYTES = 16 * 1024 * 1024

#: Names that parse cleanly as numbers but measure nothing about the run.
#: The counters (`frame`, `iter`, `step`, `run`) are here because they only ever
#: draw a straight ramp: they are the x-axis of the other metrics, not a result.
SKIP_KEYS = {
    "pid", "ppid", "port", "id", "uid", "gid", "line", "lineno", "index", "idx",
    "version", "seed", "rank", "world_size", "device", "gpu", "cuda", "year",
    "month", "day", "hour", "minute", "second", "date", "time", "timestamp",
    "epoch_time", "argv", "exit", "status", "code", "level", "thread", "worker",
    "frame", "frame_id", "iter", "iteration", "step", "run", "run_id", "seq",
    "sample", "sample_id", "batch_idx", "n", "i",
}

#: The loose "name number unit" pattern only fires for units that are actually
#: measurements, which is what keeps `Loading model 3` out of the charts.
KNOWN_UNITS = {
    "ms", "us", "µs", "ns", "s", "sec", "secs", "second", "seconds", "min",
    "fps", "hz", "khz", "mhz", "ghz",
    "b", "kb", "mb", "gb", "kib", "mib", "gib", "bytes",
    "bps", "kbps", "mbps", "gbps", "mb/s", "gb/s", "kb/s",
    "%", "flops", "gflops", "mflops", "tflops", "w", "mw", "kw", "j", "mj",
    "c", "°c", "k", "px", "frames", "imgs", "img/s", "it/s", "samples/s",
}

#: A logger's severity prefix, with or without brackets.
_LEVEL = re.compile(
    r"^[\[\(]?(?:INFO|DEBUG|WARN|WARNING|ERROR|ERR|TRACE|FATAL|CRITICAL|NOTICE)"
    r"[\]\)]?[\s:>|\-]*",
    re.IGNORECASE,
)

#: `2026-07-28 14:32:01,123` / `[14:32:01.123]` / `2026-07-28T14:32:01`
_TS = re.compile(
    r"(?P<date>\d{4}-\d{2}-\d{2})?[T\s\[]*"
    r"(?P<h>\d{2}):(?P<m>\d{2}):(?P<s>\d{2})(?:[.,](?P<frac>\d{1,6}))?"
)

#: A number that is not the first field of a date, time, version or IP: the
#: lookahead rejects `2026-`, `14:`, `2.1.0` and `192.168.…` at the source.
_NUM = r"[-+]?\d[\d_]*(?:\.\d+)?(?:[eE][-+]?\d+)?(?![\d]*[.:]\d)(?!-\d)"
_UNIT = r"[A-Za-zµ°%][A-Za-zµ°%/]{0,9}"

#: Every token starts with a letter, so `192.168.1.10` cannot be swept into a
#: key and `INFO starting … on 192.168.1.10:5672` stops being "a metric".
#: At most three words: past that it is a sentence, not a name.
_KEY = r"[A-Za-z][\w.\-]{0,30}(?:[ ][A-Za-z][\w.\-]{0,30}){0,2}"

#: The unit is optional, and the trailing lookahead is what makes it safe: on
#: `edge_ms=12.4 msg_mb=0.19` a greedy unit swallows ` msg`, leaving `_mb=0.19`
#: to be re-read as a metric called `mb`. Requiring the unit to end at a
#: non-word, non-assignment character makes it backtrack to empty instead.
_UNIT_TAIL = rf"(?:\s*({_UNIT}))?(?![\w=:])"

#: `key=12.4ms`, `key = 12.4 ms`
_KV_EQ = re.compile(rf"({_KEY})\s*=\s*({_NUM}){_UNIT_TAIL}")
#: `key: 12.4 ms` -- the colon form, which is what most profilers print.
_KV_COLON = re.compile(rf"({_KEY})\s*:\s*({_NUM}){_UNIT_TAIL}")
#: `Total inference      12.34 ms` -- column-aligned profiler tables. Anchored
#: at both ends, so the unit cannot over-run into anything.
_ALIGNED = re.compile(rf"^\s*([A-Za-z][\w .\-()/]{{0,48}}?)\s{{2,}}({_NUM})\s*({_UNIT})?\s*$")
#: `FPS 21.3` -- the loosest form. A bare name and number is far more often
#: prose (`epoch 3 done`) than a measurement, so this one only counts when the
#: trailing word is a real unit; `_candidates` marks it `strict` for that check.
_LOOSE = re.compile(rf"^\s*([A-Za-z][\w .\-()/]{{0,40}}?)\s+({_NUM})\s*({_UNIT})\s*$")


def num(raw: str) -> float | None:
    """The one place a numeric token becomes a float.

    Percent-formatted values (`utilization=37.82%`) are the common case the
    guide calls out: strip the sign here rather than at every call site.
    """
    token = (raw or "").strip().replace("_", "").rstrip("%")
    if not token:
        return None
    try:
        value = float(token)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def slug(name: str) -> str:
    """`Total Inference (ms)` -> `total_inference`. Units live on the sample."""
    text = re.sub(r"\(.*?\)", " ", name or "")
    text = re.sub(r"[^0-9A-Za-z]+", "_", text).strip("_").lower()
    text = re.sub(r"_{2,}", "_", text)
    # A trailing unit in the name duplicates `Sample.unit`; keep the name clean
    # so `edge_ms` from one file and `edge` from another still line up.
    return text


def _label(name: str) -> str:
    """Slug -> the words a chart title should say."""
    return (name or "").replace("_", " ").strip().capitalize() or "value"


# ------------------------------------------------------------------ data model
@dataclass(slots=True)
class Sample:
    metric: str
    value: float
    unit: str = ""
    source: str = ""
    line: int = 0
    order: int = 0
    at: float | None = None  # seconds from the file's first timestamp


@dataclass
class Metric:
    """Every reading of one name, kept grouped by the file it came from."""

    name: str
    unit: str = ""
    by_source: dict[str, list[Sample]] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return _label(self.name)

    @property
    def sources(self) -> list[str]:
        return list(self.by_source)

    @property
    def samples(self) -> list[Sample]:
        return [s for rows in self.by_source.values() for s in rows]

    @property
    def count(self) -> int:
        return sum(len(rows) for rows in self.by_source.values())

    @property
    def values(self) -> list[float]:
        return [s.value for s in self.samples]

    @property
    def per_source_counts(self) -> list[int]:
        return [len(rows) for rows in self.by_source.values()]

    @property
    def is_scalar(self) -> bool:
        """One reading per file -- a comparison, not a trend."""
        return bool(self.by_source) and max(self.per_source_counts) == 1

    @property
    def has_timeline(self) -> bool:
        return any(s.at is not None for s in self.samples)

    @property
    def spread(self) -> float:
        vals = self.values
        return (max(vals) - min(vals)) if vals else 0.0


@dataclass
class FileInfo:
    path: str
    bytes: int
    lines: int = 0
    matches: int = 0
    kind: str = "log"
    error: str = ""


@dataclass
class ParseResult:
    metrics: dict[str, Metric] = field(default_factory=dict)
    files: list[FileInfo] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def read_files(self) -> list[FileInfo]:
        return [f for f in self.files if not f.error]

    def ranked(self) -> list[Metric]:
        """Most chartable first: many samples, or spread across many files."""
        return sorted(
            self.metrics.values(),
            key=lambda m: (len(m.by_source) > 1, m.count, m.spread > 0),
            reverse=True,
        )


# --------------------------------------------------------------------- parsing
def _timestamp(line: str) -> float | None:
    m = _TS.search(line[:40])
    if not m:
        return None
    frac = m.group("frac") or "0"
    return (
        int(m.group("h")) * 3600
        + int(m.group("m")) * 60
        + int(m.group("s"))
        + float(f"0.{frac}")
    )


def _candidates(line: str) -> Iterator[tuple[str, str, str, bool]]:
    """(name, raw number, unit, unit_required) a single line can defend."""
    body = line.strip()
    if not body or body.startswith(("#", "//")):
        return

    # A leading timestamp is the sample's clock, never a measurement.
    ts = _TS.search(body[:40])
    if ts and ts.start() < 3:
        body = body[ts.end():].lstrip(" ]|-\t")

    # …and a leading severity is the logger's, not part of the first key:
    # `INFO frame=1` otherwise reads as a metric called `info_frame`, which
    # slips past the `frame` counter filter.
    level = _LEVEL.match(body)
    if level:
        body = body[level.end():]

    hits = 0
    for pattern in (_KV_EQ, _KV_COLON):
        for m in pattern.finditer(body):
            hits += 1
            yield m.group(1), m.group(2), (m.group(3) or ""), False
    if hits:
        return

    m = _ALIGNED.match(body)
    if m:
        yield m.group(1), m.group(2), (m.group(3) or ""), False
        return
    m = _LOOSE.match(body)
    if m:
        yield m.group(1), m.group(2), (m.group(3) or ""), True


def parse_log(text: str, source: str) -> tuple[list[Sample], int]:
    """Extract samples from one plain-text log. Returns (samples, lines read)."""
    out: list[Sample] = []
    seen = Counter()
    base_ts: float | None = None
    lines = 0

    for lineno, line in enumerate(text.splitlines(), start=1):
        lines = lineno
        at = _timestamp(line)
        if at is not None:
            if base_ts is None:
                base_ts = at
            # Logs that cross midnight would otherwise jump backwards a day.
            at = at - base_ts + (86400.0 if at < base_ts else 0.0)

        for raw_name, raw_value, raw_unit, unit_required in _candidates(line):
            name = slug(raw_name)
            if not name or name in SKIP_KEYS or len(name) > 48:
                continue
            value = num(raw_value)
            if value is None:
                continue
            unit = (raw_unit or "").strip()
            if raw_value.rstrip().endswith("%"):
                unit = "%"
            known = unit.lower() in KNOWN_UNITS
            if unit_required and not known:
                # `epoch 3 done` is a sentence. Without a real unit the loose
                # pattern has nothing to distinguish it from a measurement.
                continue
            if not known:
                # An unknown trailing word is prose -- keep the number, drop it.
                unit = ""
            seen[name] += 1
            out.append(
                Sample(metric=name, value=value, unit=unit, source=source,
                       line=lineno, order=seen[name] - 1, at=at)
            )
    return out, lines


def parse_table(text: str, source: str) -> tuple[list[Sample], int]:
    """CSV/TSV: every numeric column becomes a metric, one sample per row."""
    sample = text[:8192]
    try:
        dialect: Any = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    out: list[Sample] = []
    rows = 0
    for row_i, row in enumerate(reader):
        rows = row_i + 1
        for key, raw in row.items():
            if key is None or raw is None:
                continue
            name = slug(key)
            if not name or name in SKIP_KEYS:
                continue
            value = num(str(raw))
            if value is None:
                continue
            out.append(
                Sample(metric=name, value=value, unit="%" if str(raw).strip().endswith("%") else "",
                       source=source, line=row_i + 2, order=row_i)
            )
    return out, rows


def _walk_json(node: Any, prefix: str = "") -> Iterator[tuple[str, float]]:
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _walk_json(value, f"{prefix}_{key}" if prefix else str(key))
    elif isinstance(node, list):
        for item in node:
            yield from _walk_json(item, prefix)
    elif isinstance(node, bool):
        return
    elif isinstance(node, (int, float)) and math.isfinite(float(node)):
        yield prefix, float(node)


def parse_json(text: str, source: str) -> tuple[list[Sample], int]:
    """JSON or JSONL. Nested keys flatten to `outer_inner`."""
    docs: list[Any] = []
    stripped = text.strip()
    if not stripped:
        return [], 0
    try:
        docs = [json.loads(stripped)]
    except ValueError:
        for line in stripped.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                docs.append(json.loads(line))
            except ValueError:
                continue

    out: list[Sample] = []
    seen = Counter()
    for doc_i, doc in enumerate(docs):
        for key, value in _walk_json(doc):
            name = slug(key)
            if not name or name in SKIP_KEYS:
                continue
            seen[name] += 1
            out.append(Sample(metric=name, value=value, source=source,
                              line=doc_i + 1, order=seen[name] - 1))
    return out, len(docs)


def _kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in (".csv", ".tsv"):
        return "table"
    if suffix in (".json", ".jsonl", ".ndjson"):
        return "json"
    return "log"


def readable(path: Path) -> bool:
    if path.suffix.lower() in TEXT_SUFFIXES:
        return True
    if not path.suffix:
        try:
            return path.stat().st_size <= NO_SUFFIX_MAX_BYTES
        except OSError:
            return False
    return False


def parse_tree(root: Path) -> ParseResult:
    """Read every text-ish file under `root` and merge what they measured."""
    result = ParseResult()
    metrics = result.metrics
    units: dict[str, Counter] = {}

    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if not readable(path):
            result.skipped.append(rel)
            continue

        info = FileInfo(path=rel, bytes=size, kind=_kind(path))
        result.files.append(info)
        try:
            raw = path.read_bytes()[:MAX_FILE_BYTES]
            text = raw.decode("utf-8", errors="replace")
        except OSError as exc:
            info.error = str(exc)
            continue
        if "\x00" in text[:4096]:
            info.error = "binary"
            result.skipped.append(rel)
            result.files.remove(info)
            continue

        parser = {"table": parse_table, "json": parse_json}.get(info.kind, parse_log)
        try:
            samples, lines = parser(text, rel)
        except Exception as exc:  # noqa: BLE001 - one bad file must not sink the report
            log.warning("parsing %s failed: %s", rel, exc)
            info.error = f"unreadable: {exc}"
            continue

        info.lines = lines
        info.matches = len(samples)
        for s in samples:
            metric = metrics.get(s.metric)
            if metric is None:
                metric = metrics[s.metric] = Metric(name=s.metric)
                units[s.metric] = Counter()
            metric.by_source.setdefault(s.source, []).append(s)
            if s.unit:
                units[s.metric][s.unit] += 1

    for name, counter in units.items():
        if counter:
            metrics[name].unit = counter.most_common(1)[0][0]

    if not result.files:
        result.warnings.append("no readable text files in that directory")
    elif not metrics:
        result.warnings.append(
            f"read {len(result.files)} file(s) but found no `name value` measurements"
        )
    return result
