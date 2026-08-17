"""The project queue: the saved list of projects, and the one button that runs it.

Nothing here goes near the machines itself — `services/project_queue.py` drives
the same `ssh/commands.py` fan-out the Control tab does. This layer is the
security boundary and the editor's storage, in that order.
"""

from __future__ import annotations

import logging
from pathlib import PurePosixPath
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from ..auth import require_token
from ..db import get_session
from ..models import CommandPreset, QueueProject
from ..schemas import QueueProjectsIn, QueueStartRequest
from ..services.notify import notifier
from ..services.project_queue import QueueError, runner
from ..services.server_state import server_state
from ..ssh import commands as cmds
from ..ssh.commands import CommandRejected

log = logging.getLogger(__name__)

router = APIRouter(prefix="/queue", tags=["queue"], dependencies=[Depends(require_token)])


def _row_out(r: QueueProject) -> dict[str, Any]:
    return {
        "name": r.name,
        "path": r.path,
        "enabled": r.enabled,
        "expected_s": r.expected_s,
        "overrides": dict(r.overrides or {}),
    }


@router.get("/projects")
async def list_projects(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """The editable project list, in run order."""
    rows = (
        await session.exec(select(QueueProject).order_by(QueueProject.position))
    ).all()
    return {"projects": [_row_out(r) for r in rows]}


@router.put("/projects")
async def save_projects(
    payload: QueueProjectsIn, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """Replace the whole list — the editor sends what it has, in its order.

    A path here only ever reaches `cd` (via `with_working_directory`, which
    `shlex.quote`s it), so it grants nothing on its own. An *override command*
    does run, so it goes through the same allow-list as anything typed into the
    Control tab's command box; refusing it here means finding out while the
    editor is open rather than at project four of six.
    """
    saved = {p.command for p in (await session.exec(select(CommandPreset))).all()}
    for item in payload.projects:
        if not item.path.strip():
            continue
        for target, command in (item.overrides or {}).items():
            if not (command or "").strip():
                continue
            try:
                resolved = cmds.substitute_broker_ip(command, server_state.host)
                cmds.reject_if_interactive(resolved)
                cmds.validate_command(resolved, saved=saved)
            except CommandRejected as exc:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"{item.name or item.path} → {target}: {exc}",
                ) from exc

    for row in (await session.exec(select(QueueProject))).all():
        await session.delete(row)

    for i, item in enumerate(payload.projects):
        path = item.path.strip()
        if not path:
            continue
        # Default the name to the directory's last segment: `split_inference_test`
        # is what tells one project from another, and the full path is right
        # beside it in the editor anyway.
        name = item.name.strip() or PurePosixPath(path).name or path
        session.add(
            QueueProject(
                name=name,
                path=path,
                enabled=item.enabled,
                position=i,
                expected_s=max(0, item.expected_s),
                overrides={k: v for k, v in (item.overrides or {}).items() if v.strip()},
            )
        )

    await session.commit()
    rows = (
        await session.exec(select(QueueProject).order_by(QueueProject.position))
    ).all()
    log.info("queue projects updated: %d entries", len(rows))
    return {"projects": [_row_out(r) for r in rows]}


@router.get("/status")
async def queue_status() -> dict[str, Any]:
    """The active queue (or null), the resolved plan, and whether Telegram is on."""
    return runner.status()


@router.post("/start")
async def start(payload: QueueStartRequest | None = None) -> dict[str, Any]:
    """Run the saved projects, one after another.

    Answers as soon as the queue is running — it then reports over `/ws/stream`
    and Telegram for however many hours it takes.
    """
    body = payload or QueueStartRequest()
    try:
        run = await runner.start(
            only=body.only,
            cleanup=body.cleanup,
            budget_s=body.budget_s,
            notify=body.notify,
            notify_steps=body.notify_steps,
        )
    except QueueError as exc:
        # 409 for "something is already running", 400 for a plan that cannot be
        # built (no projects, no server login, no command saved for a stage).
        already = "already running" in str(exc)
        raise HTTPException(
            status.HTTP_409_CONFLICT if already else status.HTTP_400_BAD_REQUEST, str(exc)
        ) from exc
    return {"run": run.to_dict(), "notify": notifier.status()}


@router.post("/stop")
async def stop() -> dict[str, Any]:
    """Ctrl-C the current project and cancel the rest of the queue."""
    return await runner.stop()
