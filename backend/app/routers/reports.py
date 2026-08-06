"""Pull a result directory, chart it, and keep the review (guides/visual_guide.md).

The Control tab's Files card already pulls one file. This is the other half of
what happens when a run finishes: point at the directory the run wrote, and get
back a set of charts drawn to the visual guide, each with a box for the short
note that explains what it showed.

Analysis is deliberately synchronous. It takes seconds, not minutes, and a
progress-polling protocol for something that fast buys nothing but a second way
for the UI and the server to disagree about what happened.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import asyncssh
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlmodel.ext.asyncio.session import AsyncSession

from ..auth import require_token
from ..db import get_session
from ..reports import charts as chartmod
from ..reports import parse as parsemod
from ..reports import store
from ..schemas import AnalyzeRequest, NotesIn, ViewsIn
from ..services.metrics_bus import bus
from ..ssh import commands as cmds
from ..ssh.pool import SSHError, pool
# The reserved-server resolution and the remote-path guard are the Control
# tab's rules, and a second copy of either would be a second thing to keep in
# step with `ssh/gateway.py`.
from .control import _check_remote_path, _resolve_devices

log = logging.getLogger(__name__)

router = APIRouter(prefix="/reports", tags=["reports"], dependencies=[Depends(require_token)])


def _analyse(
    pulled_root: Path,
    out_dir: Path,
    views: dict[str, chartmod.View] | None = None,
    window: chartmod.Window | None = None,
) -> tuple[Any, list[Any], list[Any], list[str]]:
    """Parse + render. Runs in a worker thread: matplotlib is CPU-bound and
    would otherwise stall every other request on the event loop."""
    parsed = parsemod.parse_tree(pulled_root)
    # `source_dir` lets the renderer recognise one of the server's own result
    # directories and draw the guide's catalogue against its real schema,
    # instead of the file-by-file view the schema-blind parser can offer.
    drawn, tiles, notes = chartmod.render(
        parsed, out_dir, source_dir=pulled_root, views=views, window=window
    )
    return parsed, drawn, tiles, notes


@router.post("/analyze")
async def analyze(
    payload: AnalyzeRequest, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """Pull `path` off a device, chart what it measured, save it as a report."""
    target = _check_remote_path(payload.path)
    device = (await _resolve_devices(session, [payload.device_id]))[0]
    window = chartmod.Window.of(payload.window_start, payload.window_end)

    when = datetime.now()
    report_id = store.report_id(payload.case_name, when)
    directory = store.report_dir(report_id)
    if directory.exists():
        # Same case name inside the same minute: the operator is re-running,
        # and the newer analysis is the one they want to see.
        shutil.rmtree(directory, ignore_errors=True)

    staging = Path(tempfile.mkdtemp(prefix="split-report-"))
    bus.exec_line(payload.device_id, f"⇩ pulling {target} for analysis…", "meta")
    try:
        try:
            conn = await pool.get(device)
            pulled = await cmds.sftp_get_dir(
                conn, target, staging, suffixes=parsemod.TEXT_SUFFIXES
            )
        except SSHError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
        except NotADirectoryError as exc:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"{target} is a file — pick the directory the run wrote",
            ) from exc
        except (OSError, asyncssh.Error) as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{target}: {exc}") from exc

        if not pulled["files"]:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"{target} has no readable result files "
                f"({len(pulled['skipped'])} entries skipped as non-text)",
            )

        bus.exec_line(
            payload.device_id,
            f"   pulled {len(pulled['files'])} file(s), "
            f"{pulled['bytes'] / 1e6:.1f} MB — charting",
            "stdout",
        )
        # The whole directory is pulled and kept whatever the window is: the
        # window says which readings to *chart*, and throwing the rest away
        # would make widening it later mean another trip over SSH.
        parsed, drawn, tiles, notes = await asyncio.to_thread(
            _analyse, staging, directory / store.IMG_DIR, None, window
        )
        # Kept *after* a successful render, so a failed analysis leaves no
        # half-report behind, and before the staging dir goes.
        kept, kept_bytes = await asyncio.to_thread(store.keep_logs, report_id, staging)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    warnings = list(parsed.warnings) + list(notes)
    if pulled["truncated"]:
        warnings.append(f"only part of the directory was read: {pulled['truncated']}")
    if kept < len(pulled["files"]):
        warnings.append(
            f"{kept} of {len(pulled['files'])} log(s) kept with the report; "
            "charts cannot be re-drawn from the rest"
        )

    report = store.Report(
        id=report_id,
        case_name=payload.case_name.strip() or "untitled",
        created_at=when.isoformat(timespec="seconds"),
        label=when.strftime(store.LABEL_FORMAT),
        rendered_at=when.isoformat(timespec="seconds"),
        device_id=payload.device_id,
        device_name=device.name,
        source_path=target,
        charts=[dict(c.to_dict(), note="") for c in drawn],
        tiles=[t.to_dict() for t in tiles],
        files=[
            {"path": f.path, "bytes": f.bytes, "lines": f.lines,
             "matches": f.matches, "kind": f.kind}
            for f in parsed.read_files
        ],
        metrics=[m.name for m in parsed.ranked()],
        warnings=warnings,
        window={} if window.whole else window.to_dict(),
        pulled={
            "files": len(pulled["files"]), "bytes": pulled["bytes"],
            "skipped": len(pulled["skipped"]), "truncated": pulled["truncated"],
            "kept": kept, "kept_bytes": kept_bytes,
        },
    )
    store.write(report)
    bus.exec_line(
        payload.device_id,
        f"✓ {len(drawn)} chart(s) from {len(report.files)} file(s) → {report_id}",
        "stdout",
    )
    return report.to_dict()


@router.get("")
async def list_reports(limit: int = 200, day: str | None = None) -> dict[str, Any]:
    """Saved reports, newest first, plus the day buckets the history bar shows.

    `days` is always computed over the full listing, so selecting one day never
    hides the fact that other days have results.
    """
    rows = store.listing(max(1, min(500, limit)))
    days = store.day_groups(rows)
    if day:
        rows = [r for r in rows if r.get("day") == day]
    return {"reports": rows, "days": days}


@router.get("/{report_id}")
async def get_report(report_id: str) -> dict[str, Any]:
    if not store.is_valid_id(report_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "bad report id")
    report = store.read(report_id)
    if report is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no report {report_id}")
    return report.to_dict()


@router.put("/{report_id}/notes")
async def save_notes(report_id: str, payload: NotesIn) -> dict[str, Any]:
    """Attach the per-chart notes and the overall review, and stamp it saved."""
    if not store.is_valid_id(report_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "bad report id")
    report = store.read(report_id)
    if report is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no report {report_id}")
    return store.set_notes(report, payload.notes, payload.review).to_dict()


@router.put("/{report_id}/views")
async def save_views(report_id: str, payload: ViewsIn) -> dict[str, Any]:
    """Re-draw a report with the config panel's overrides applied.

    The charts are PNGs, so "hide that series" and "rename the y axis" are not
    client-side toggles -- the figures are drawn again from the logs kept with
    the report. That costs a couple of seconds and buys the thing worth having:
    the saved image *is* the edited chart, so it still reads correctly in six
    months, in a paper, and to anyone who was never in this browser session.

    Synchronous for the same reason `analyze` is: it takes seconds, and a
    progress protocol for that only adds a second way to disagree about what
    happened. Per-chart notes and the overall review survive untouched.
    """
    if not store.is_valid_id(report_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "bad report id")
    report = store.read(report_id)
    if report is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no report {report_id}")

    logs = store.logs_dir(report_id)
    if not logs.is_dir() or not any(logs.iterdir()):
        # Reports analysed before the logs were kept cannot be re-drawn.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "this report was saved without its logs, so its charts cannot be "
            "re-drawn — run Analyze on the same directory again",
        )

    views = {
        chart_id: chartmod.View.from_dict(view.model_dump())
        for chart_id, view in payload.views.items()
    }
    # The window came from the report, not from this request. It is a property
    # of the analysis rather than of the wording, so renaming an axis must not
    # quietly widen the charts back out to the whole run.
    parsed, drawn, tiles, notes = await asyncio.to_thread(
        _analyse, logs, store.report_dir(report_id) / store.IMG_DIR, views,
        chartmod.Window.from_dict(report.window),
    )

    report.charts = store.carry_over(report, [c.to_dict() for c in drawn])
    report.tiles = [t.to_dict() for t in tiles]
    report.metrics = [m.name for m in parsed.ranked()]
    report.warnings = list(parsed.warnings) + list(notes)
    report.rendered_at = datetime.now().isoformat(timespec="seconds")
    store.write(report)
    return report.to_dict()


@router.get("/{report_id}/imgs/{name}")
async def chart_image(report_id: str, name: str) -> FileResponse:
    """A chart PNG. Immutable under its name, so it caches hard."""
    path = store.image_path(report_id, name)
    if path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such chart")
    return FileResponse(
        path, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"}
    )


@router.delete("/{report_id}")
async def delete_report(report_id: str) -> dict[str, Any]:
    if not store.is_valid_id(report_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "bad report id")
    if not store.delete(report_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no report {report_id}")
    return {"deleted": report_id}
