"""Auto-run: launch an unattended schedule script and watch it.

Unlike `/control/*`, nothing here goes over SSH. The schedule runs **on this
machine** — the server where the project directories live — because a script
that drives a dozen projects is the conductor of the fleet, not one of its
members. See `services/autorun.py` for the tracking and notification model.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from ..auth import require_token
from ..schemas import AutoRunStartRequest, AutoRunStopRequest
from ..services.autorun import AutoRunError, runner
from ..services.notify import notifier

log = logging.getLogger(__name__)

router = APIRouter(prefix="/autorun", tags=["autorun"], dependencies=[Depends(require_token)])


@router.get("/status")
async def autorun_status() -> dict[str, Any]:
    """The active run (or null), plus whether notifications are wired up."""
    return runner.status()


@router.get("/scripts")
async def list_scripts() -> dict[str, Any]:
    """Schedule scripts available in `AUTORUN_DIR`."""
    return {"dir": str(runner.root), "scripts": runner.list_scripts()}


@router.post("/start")
async def start(payload: AutoRunStartRequest) -> dict[str, Any]:
    """Start a schedule. Returns as soon as it is running — it then reports
    progress over `/ws/stream` and Telegram for however many hours it takes."""
    try:
        run = await runner.start(
            payload.script,
            args=payload.args,
            cwd=payload.cwd,
            markers=payload.markers,
            notify=payload.notify,
            notify_steps=payload.notify_steps,
            env=payload.env,
        )
    except AutoRunError as exc:
        # 409 for "one already running", 400 for a bad script/path/argument.
        already = "already running" in str(exc)
        raise HTTPException(
            status.HTTP_409_CONFLICT if already else status.HTTP_400_BAD_REQUEST, str(exc)
        ) from exc
    return {"run": run.to_dict(), "notify": notifier.status()}


@router.post("/stop")
async def stop(payload: AutoRunStopRequest | None = None) -> dict[str, Any]:
    """Stop the active schedule, killing the whole process tree it spawned."""
    return await runner.stop(grace=(payload.grace if payload else None))


@router.get("/tail")
async def tail(limit: int = 200) -> dict[str, Any]:
    """Recent output of the active run, for a page that missed the WebSocket."""
    return {"lines": runner.tail(limit)}


@router.get("/history")
async def history(limit: int = 25) -> dict[str, Any]:
    """Past runs, newest first, read back from their saved manifests."""
    return {"runs": runner.history(limit)}


@router.get("/runs/{run_id}/log")
async def run_log(run_id: str, limit: int = 500) -> dict[str, Any]:
    try:
        return runner.read_log(run_id, limit)
    except AutoRunError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.post("/notify/test")
async def notify_test() -> dict[str, Any]:
    """Send a test message, so the wiring is verified before an overnight run
    depends on it. Reports Telegram's own error text on failure ("chat not
    found", "Unauthorized") rather than a bare false."""
    return await notifier.verify()
