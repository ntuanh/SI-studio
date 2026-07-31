"""Metrics snapshots + the UI-JSON seed import (guide §7, §10)."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from ..auth import require_token
from ..db import get_session
from ..inference import simulation as sim
from ..inference.broker import broker
from ..inference.orchestrator import orchestrator
from ..models import Cluster, Device, ModelDef
from ..schemas import GlobalConfigOut, MetricsOut, SeedRequest, SeedResponse
from ..services import topology
from ..services.metrics_bus import bus
from ..ssh import secrets_store

log = logging.getLogger(__name__)

router = APIRouter(tags=["metrics"], dependencies=[Depends(require_token)])


# ------------------------------------------------------------------- metrics
@router.get("/metrics/latest", response_model=MetricsOut)
async def latest(session: AsyncSession = Depends(get_session)) -> MetricsOut:
    """Snapshot in the §6 shape, one entry per cluster.

    A cluster with an active run reports measured values (`source: "live"`);
    everything else falls back to the simulator (`source: "sim"`) so the
    Simulation tab has data before anything is deployed.
    """
    payloads: list[dict[str, Any]] = []
    live_ids: list[int] = []

    cfg = await topology.load_global_config(session)
    extra = await topology.load_custom_models(session)

    for cl in await topology.build_clusters(session):
        if orchestrator.is_live(cl.id):
            payload = await orchestrator.live_payload(cl.id)
            if payload is not None:
                live_ids.append(cl.id)
                payloads.append(payload)
                continue

        metrics = sim.sim_cluster(
            cl,
            auto_balance=cfg.auto_balance,
            manual_enabled=cfg.manual_enabled,
            manual_split=cfg.manual_split,
            extra_models=extra,
        )
        if metrics is None:
            payloads.append(
                {
                    "cluster": cl.id,
                    "idle": True,
                    "reason": "needs at least one edge and one cloud device",
                    "queue_depth": await broker.queue_depth(cl.queue_name)
                    if broker.connected
                    else 0,
                    "devices": [],
                    "source": "sim",
                }
            )
            continue

        depth = await broker.queue_depth(cl.queue_name) if broker.connected else 0
        payloads.append(metrics.to_payload(queue_depth=depth, source="sim"))

    aggregate = sum(float(p.get("fps") or 0.0) for p in payloads)
    return MetricsOut(
        clusters=payloads,
        aggregate_fps=round(aggregate, 3),
        live_clusters=live_ids,
        generated_at=datetime.now(timezone.utc),
    )


@router.get("/metrics/stream-state")
async def stream_state() -> dict[str, Any]:
    """What /ws/stream would replay right now -- handy for debugging."""
    return {
        "subscribers": bus.subscriber_count,
        "snapshot": bus.snapshot(),
        "active_runs": orchestrator.active_runs(),
        "broker_connected": broker.connected,
    }


# ---------------------------------------------------------------------- seed
@router.post("/seed", response_model=SeedResponse)
async def seed(payload: SeedRequest, session: AsyncSession = Depends(get_session)) -> SeedResponse:
    """Import the UI's exported JSON (the **JSON** button / `exportJson()`).

    Devices arrive as `{id, name, gflops, bw, lat, cluster}` nested under stages;
    connection details aren't in the export, so `default_*` fills them in and
    hosts stay blank until the Control tab supplies them.
    """
    if payload.replace:
        for row in (await session.exec(select(Device))).all():
            secrets_store.forget_device(row.id)
            await session.delete(row)
        for row in (await session.exec(select(Cluster))).all():
            await session.delete(row)
        await session.commit()

    # --- global config ---
    cfg = await topology.load_global_config(session)
    ui_cfg = payload.config or {}
    mapping = {
        "clustering": "clustering",
        "numClusters": "num_clusters",
        "autoBalance": "auto_balance",
        "manualEnabled": "manual_enabled",
        "manualSplit": "manual_split",
        "modelName": "model_name",
    }
    for ui_key, field in mapping.items():
        if ui_key in ui_cfg and ui_cfg[ui_key] is not None:
            setattr(cfg, field, ui_cfg[ui_key])
    if payload.model:
        cfg.model_name = payload.model
    cfg.num_clusters = max(1, cfg.num_clusters)
    session.add(cfg)

    # --- uploaded model ---
    models_added = 0
    if payload.uploaded_model and payload.uploaded_model.get("layers"):
        name = str(payload.uploaded_model.get("name") or "uploaded")
        try:
            model = sim.model_from_layers(
                name, str(payload.uploaded_model.get("label") or name),
                payload.uploaded_model["layers"],
            )
        except (ValueError, TypeError) as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"bad uploaded_model: {exc}") from exc
        row = await session.get(ModelDef, name) or ModelDef(name=name)
        row.label = model.label
        row.layers = [{"name": l.name, "flops": l.flops, "bytes": l.bytes} for l in model.layers]
        session.add(row)
        models_added = 1

    # --- devices ---
    device_count = 0
    seen: set[str] = set()
    for stage in payload.stages:
        kind = str(stage.get("kind") or "Custom")
        stage_id = str(stage.get("id") or "")
        stage_name = str(stage.get("name") or kind)
        for dev in stage.get("devices") or []:
            did = str(dev.get("id") or f"d{uuid.uuid4().hex[:5]}")
            if did in seen:
                did = f"{did}-{uuid.uuid4().hex[:4]}"
            seen.add(did)

            existing = await session.get(Device, did)
            row = existing or Device(id=did)
            row.name = str(dev.get("name") or did)
            row.kind = kind
            row.cluster_id = max(1, int(dev.get("cluster") or 1))
            row.gflops = float(dev.get("gflops") or 0.0)
            row.bandwidth_mb_s = float(dev.get("bw") or dev.get("bandwidth_mb_s") or 0.0)
            row.latency_ms = float(dev.get("lat") or dev.get("latency_ms") or 0.0)
            row.stage_id = stage_id
            row.stage_name = stage_name
            if not existing:
                row.host = str(dev.get("host") or "")
                row.port = payload.default_port
                row.username = payload.default_username
                row.auth_method = payload.default_auth_method
                row.key_ref = payload.default_key_ref
            session.add(row)
            device_count += 1

    await session.commit()

    # --- per-cluster config (`clusters[]` from the export, or `clusterCfg`) ---
    cluster_count = 0
    for entry in payload.clusters:
        cid = int(entry.get("id") or 0)
        if cid <= 0:
            continue
        conf = entry.get("config") or {}
        row = await session.get(Cluster, cid) or Cluster(id=cid)
        row.queue_name = str(entry.get("queue") or f"intermediate_queue_{cid}")
        row.model_name = str(conf.get("modelName") or cfg.model_name)
        row.batch_size = int(conf.get("batchSize") or 32)
        row.num_bit = int(conf.get("numBit") or 8)
        override = conf.get("splitOverride")
        row.cut_layer = int(override) if override not in (None, "") else None
        session.add(row)
        cluster_count += 1

    for cid_raw, conf in (payload.cluster_cfg or {}).items():
        try:
            cid = int(cid_raw)
        except (TypeError, ValueError):
            continue
        row = await session.get(Cluster, cid) or Cluster(id=cid)
        row.ensure_queue_name()
        if conf.get("modelName"):
            row.model_name = str(conf["modelName"])
        if conf.get("batchSize"):
            row.batch_size = int(conf["batchSize"])
        if conf.get("numBit"):
            row.num_bit = int(conf["numBit"])
        if "splitOverride" in conf:
            row.cut_layer = int(conf["splitOverride"]) if conf["splitOverride"] is not None else None
        session.add(row)
        cluster_count += 1

    await session.commit()

    # Make sure a row exists for every group the config implies.
    await topology.build_clusters(session)
    await session.refresh(cfg)

    from ..config import settings

    return SeedResponse(
        devices=device_count,
        clusters=cluster_count,
        models=models_added,
        config=GlobalConfigOut(
            clustering=cfg.clustering,
            num_clusters=cfg.num_clusters,
            auto_balance=cfg.auto_balance,
            manual_enabled=cfg.manual_enabled,
            manual_split=cfg.manual_split,
            model_name=cfg.model_name,
            max_message_mb=settings.max_message_mb,
        ),
    )


@router.get("/export")
async def export(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Round-trip of /seed: the inventory back in the UI's export shape.

    Deliberately omits hosts, usernames, key refs, and passwords -- the export
    is meant to be shareable (guide §8: never keep credentials in exported JSON).
    """
    cfg = await topology.load_global_config(session)
    devices = (await session.exec(select(Device))).all()

    stages: dict[str, dict[str, Any]] = {}
    for d in devices:
        key = d.stage_id or d.kind
        stage = stages.setdefault(
            key, {"id": key, "kind": d.kind, "name": d.stage_name or d.kind, "devices": []}
        )
        stage["devices"].append(
            {
                "id": d.id, "name": d.name, "gflops": d.gflops,
                "bw": d.bandwidth_mb_s, "lat": d.latency_ms, "cluster": d.cluster_id,
            }
        )

    clusters = []
    for cl in await topology.build_clusters(session):
        m = await topology.simulate_cluster(session, cl.id)
        clusters.append(
            {
                "id": cl.id,
                "queue": cl.queue_name,
                "config": {
                    "modelName": cl.model_name,
                    "batchSize": cl.batch_size,
                    "numBit": cl.num_bit,
                    "splitOverride": cl.split_override,
                },
                "edges": [d.name for d in cl.edges],
                "clouds": [d.name for d in cl.clouds],
                "metrics": None
                if m is None
                else {
                    "cut": m.cut,
                    "edgeMs": round(m.edge_ms, 2),
                    "transferMs": round(m.transfer_ms, 2),
                    "cloudMs": round(m.cloud_ms, 2),
                    "e2eMs": round(m.e2e_ms, 2),
                    "msgMB": round(m.msg_mb, 3),
                    "fps": round(m.fps, 2),
                },
            }
        )

    return {
        "model": cfg.model_name,
        "config": {
            "clustering": cfg.clustering,
            "numClusters": cfg.num_clusters,
            "autoBalance": cfg.auto_balance,
            "manualEnabled": cfg.manual_enabled,
            "manualSplit": cfg.manual_split,
            "modelName": cfg.model_name,
        },
        "stages": list(stages.values()),
        "clusters": clusters,
    }
