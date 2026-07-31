"""Deploy + split-inference run lifecycle (guide §7)."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import desc, select
from sqlmodel.ext.asyncio.session import AsyncSession

from ..auth import require_token
from ..db import get_session
from ..inference.broker import BrokerUnavailable, broker
from ..inference.orchestrator import orchestrator
from ..models import Run
from ..schemas import DeployRequest, StartRequest, StopRequest
from ..services import topology

log = logging.getLogger(__name__)

router = APIRouter(prefix="/run", tags=["run"], dependencies=[Depends(require_token)])


async def _target_clusters(session: AsyncSession, cluster_id: int | None) -> list[int]:
    """None -> every cluster that has both an edge and a cloud device."""
    if cluster_id is not None:
        return [cluster_id]
    ids = [cl.id for cl in await topology.build_clusters(session) if cl.edges and cl.clouds]
    if not ids:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "no runnable cluster: each needs at least one edge and one cloud device",
        )
    return ids


@router.post("/deploy")
async def deploy(
    payload: DeployRequest, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """SCP the shards + agent code and (optionally) install dependencies."""
    results = []
    for cid in await _target_clusters(session, payload.cluster_id):
        try:
            results.append(
                await orchestrator.deploy(
                    session,
                    cid,
                    install_deps=payload.install_deps,
                    head_shard=payload.head_shard,
                    tail_shard=payload.tail_shard,
                )
            )
        except FileNotFoundError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return {"clusters": results, "ok": all(r["ok"] for r in results)}


@router.post("/start")
async def start(
    payload: StartRequest, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """Begin split inference. The UI's "Run simulation → live" path lands here."""
    started, errors = [], []
    for cid in await _target_clusters(session, payload.cluster_id):
        try:
            started.append(
                await orchestrator.start(session, cid, source="ui", max_frames=payload.max_frames)
            )
        except BrokerUnavailable as exc:
            # Nothing can run without the broker, so fail the whole call loudly
            # rather than reporting it as a per-cluster problem.
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
        except (ValueError, RuntimeError) as exc:
            errors.append({"cluster": cid, "error": str(exc)})

    if not started:
        raise HTTPException(
            status.HTTP_409_CONFLICT if errors else status.HTTP_400_BAD_REQUEST,
            "; ".join(e["error"] for e in errors) or "nothing to start",
        )
    return {"started": started, "errors": errors, "active": orchestrator.active_runs()}


@router.post("/stop")
async def stop(
    payload: StopRequest, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """Stop agents and drain queues. With no cluster_id, stops every run."""
    if payload.cluster_id is None:
        stopped = await orchestrator.stop_all(session)
    else:
        stopped = [await orchestrator.stop(session, payload.cluster_id, drain=payload.drain)]
    return {"stopped": stopped, "active": orchestrator.active_runs()}


@router.get("/active")
async def active() -> dict[str, Any]:
    return {"runs": orchestrator.active_runs(), "broker_connected": broker.connected}


@router.get("/history")
async def history(limit: int = 25, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    rows = (
        await session.exec(select(Run).order_by(desc(Run.started_at)).limit(max(1, min(200, limit))))
    ).all()
    return {
        "runs": [
            {
                "run_id": r.id,
                "cluster": r.cluster_id,
                "status": r.status,
                "model_name": r.model_name,
                "cut_layer": r.cut_layer,
                "num_bit": r.num_bit,
                "batch_size": r.batch_size,
                "started_at": r.started_at,
                "stopped_at": r.stopped_at,
                "detail": r.detail,
            }
            for r in rows
        ]
    }
