"""Saved reports on disk: one directory per report, named for when it was run.

A report is a folder, not a database row. The charts are PNGs, the manifest is
JSON, and both survive this service being stopped, moved or rebuilt -- which is
the point of saving a result in the first place. Copying the folder to a paper
directory is the whole export story.

    reports/
      2807-1432_cut6-8bit/
        manifest.json
        imgs/01_stage_breakdown.png
        imgs/02_comparison.png
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ..config import settings

log = logging.getLogger(__name__)

MANIFEST = "manifest.json"
IMG_DIR = "imgs"
#: The logs the analysis was drawn from, kept beside the charts.
#:
#: Two reasons. Re-drawing a chart with different wording or a series switched
#: off means running the renderer again, and re-pulling over SSH would make that
#: depend on a device still being reachable. And a report that carries its own
#: inputs is the export story this module is built on -- copying the folder to a
#: paper directory takes the evidence with it.
LOG_DIR = "logs"

#: A result directory is tens of KB. This is the ceiling for a directory that
#: turns out not to be one, so a stray 200 MB pull cannot fill the disk one
#: report at a time.
MAX_KEPT_BYTES = 32 * 1024 * 1024

#: `28 Jul 14:32` for a person, `2807-1432` for the folder. Day-month-hour-minute
#: is the order asked for; the human label carries the month name so the two
#: leading numbers can never be read as a month and a day.
ID_FORMAT = "%d%m-%H%M"
LABEL_FORMAT = "%d %b %H:%M"

_SAFE = re.compile(r"[^0-9A-Za-z._-]+")


def slug_case(name: str) -> str:
    """Case-test name -> a filename component. Never empty, never a path."""
    cleaned = _SAFE.sub("-", (name or "").strip()).strip("-.")
    return (cleaned or "untitled")[:48]


def report_id(case_name: str, when: datetime | None = None) -> str:
    when = when or datetime.now()
    return f"{when.strftime(ID_FORMAT)}_{slug_case(case_name)}"


def is_valid_id(value: str) -> bool:
    """Guard for a path segment that arrives in a URL."""
    return bool(value) and "/" not in value and "\\" not in value and ".." not in value


def stamp(created_at: str) -> tuple[str, str, str]:
    """ISO timestamp -> (sortable day, `28 Jul`, `14:32`).

    Sortable and human are kept apart deliberately: `2807` sorts before `0108`
    but is a month later, so anything that orders history has to use the ISO
    day and never the label.
    """
    try:
        when = datetime.fromisoformat(created_at)
    except (TypeError, ValueError):
        return "", "", ""
    return when.strftime("%Y-%m-%d"), when.strftime("%d %b"), when.strftime("%H:%M")


def day_groups(rows: list[dict[str, Any]], today: date | None = None) -> list[dict[str, Any]]:
    """Day buckets for the history bar, newest first.

    Counted over everything that was listed, *before* any day filter, so the
    bar keeps showing which other days have results once one is selected.
    """
    now = today or date.today()
    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        day = row.get("day") or ""
        if not day:
            continue
        group = groups.setdefault(
            day, {"day": day, "label": row.get("day_label") or day, "count": 0}
        )
        group["count"] += 1

    out = sorted(groups.values(), key=lambda g: g["day"], reverse=True)
    for group in out:
        try:
            delta = (now - date.fromisoformat(group["day"])).days
        except ValueError:
            continue
        if delta == 0:
            group["label"] = "Today"
        elif delta == 1:
            group["label"] = "Yesterday"
    return out


def root() -> Path:
    path = settings.reports_path
    path.mkdir(parents=True, exist_ok=True)
    return path


def report_dir(report: str) -> Path:
    return root() / report


def logs_dir(report: str) -> Path:
    return report_dir(report) / LOG_DIR


def keep_logs(report: str, pulled_root: Path) -> tuple[int, int]:
    """Copy the pulled logs into the report. Returns (files kept, bytes kept).

    Stops at `MAX_KEPT_BYTES` rather than refusing outright: the first N MB of
    an over-large directory still lets most charts be re-drawn, and the caller
    records that it was partial.
    """
    target = logs_dir(report)
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)

    kept = total = 0
    for path in sorted(p for p in pulled_root.rglob("*") if p.is_file()):
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if total + size > MAX_KEPT_BYTES:
            break
        destination = target / path.relative_to(pulled_root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(path, destination)
        except OSError as exc:
            log.warning("could not keep %s with the report: %s", path.name, exc)
            continue
        kept += 1
        total += size
    return kept, total


@dataclass
class Report:
    """The manifest, with the accessors the router needs."""

    id: str
    case_name: str = ""
    created_at: str = ""
    label: str = ""
    device_id: str = ""
    device_name: str = ""
    source_path: str = ""
    charts: list[dict[str, Any]] = field(default_factory=list)
    tiles: list[dict[str, Any]] = field(default_factory=list)
    files: list[dict[str, Any]] = field(default_factory=list)
    metrics: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    pulled: dict[str, Any] = field(default_factory=dict)
    review: str = ""
    saved_at: str = ""
    #: When the PNGs were last drawn. The chart images are cached hard under
    #: their file name, which was safe while a name meant one fixed image;
    #: editing a chart re-draws it in place, so the browser needs this to know
    #: the bytes changed.
    rendered_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "case_name": self.case_name, "created_at": self.created_at,
            "label": self.label, "device_id": self.device_id,
            "device_name": self.device_name, "source_path": self.source_path,
            "charts": self.charts, "tiles": self.tiles, "files": self.files,
            "metrics": self.metrics, "warnings": self.warnings, "pulled": self.pulled,
            "review": self.review, "saved_at": self.saved_at,
            "rendered_at": self.rendered_at,
        }

    def summary(self) -> dict[str, Any]:
        """The row shape the saved-report list renders."""
        noted = sum(1 for c in self.charts if (c.get("note") or "").strip())
        day, day_label, time_label = stamp(self.created_at)
        return {
            "id": self.id, "case_name": self.case_name, "label": self.label,
            "created_at": self.created_at, "device_name": self.device_name,
            "source_path": self.source_path, "charts": len(self.charts),
            "notes": noted, "saved_at": self.saved_at,
            "rendered_at": self.rendered_at,
            "reviewed": bool(self.review.strip()) or noted > 0,
            # Split out so the history bar can group by day and label a run by
            # its time, instead of re-parsing a formatted string in the browser.
            "day": day, "day_label": day_label, "time_label": time_label,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Report":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


def write(report: Report) -> Report:
    directory = report_dir(report.id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / MANIFEST).write_text(
        json.dumps(report.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return report


def read(report_id_: str) -> Report | None:
    manifest = report_dir(report_id_) / MANIFEST
    if not manifest.is_file():
        return None
    try:
        return Report.from_dict(json.loads(manifest.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError) as exc:
        log.warning("unreadable manifest for %s: %s", report_id_, exc)
        return None


def listing(limit: int = 200) -> list[dict[str, Any]]:
    """Newest first. Sorted by the recorded timestamp, not by folder name --
    `2807` sorts before `0108` but is a month later."""
    rows = []
    for directory in root().iterdir():
        if not directory.is_dir():
            continue
        report = read(directory.name)
        if report is not None:
            rows.append(report)
    rows.sort(key=lambda r: (r.created_at, r.id), reverse=True)
    return [r.summary() for r in rows[:limit]]


def image_path(report_id_: str, name: str) -> Path | None:
    """Resolve a chart PNG, refusing anything that escapes the report."""
    if not is_valid_id(report_id_) or "/" in name or "\\" in name or ".." in name:
        return None
    base = (report_dir(report_id_) / IMG_DIR).resolve()
    try:
        target = (base / name).resolve()
        target.relative_to(base)
    except (ValueError, OSError):
        return None
    return target if target.is_file() else None


def views_of(report: Report) -> dict[str, dict[str, Any]]:
    """The per-chart overrides currently stored, keyed by chart id."""
    return {
        str(chart.get("id", "")): dict(chart.get("view") or {})
        for chart in report.charts
        if chart.get("view")
    }


def carry_over(report: Report, drawn: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Re-drawn charts, keeping the note that was written against each one.

    A re-render builds fresh chart records, so without this a config change
    would quietly wipe the notes -- which are the only part of a report a person
    typed.
    """
    notes = {str(c.get("id", "")): c.get("note", "") for c in report.charts}
    return [dict(c, note=notes.get(str(c.get("id", "")), "")) for c in drawn]


def set_notes(report: Report, notes: dict[str, str], review: str | None) -> Report:
    """Attach the operator's short reviews and stamp the report as saved."""
    for chart in report.charts:
        text = notes.get(str(chart.get("id", "")))
        if text is not None:
            chart["note"] = text.strip()[:2000]
    if review is not None:
        report.review = review.strip()[:4000]
    report.saved_at = datetime.now().isoformat(timespec="seconds")
    return write(report)


def delete(report_id_: str) -> bool:
    directory = report_dir(report_id_)
    if not directory.is_dir():
        return False
    shutil.rmtree(directory, ignore_errors=True)
    return not directory.exists()
