"""Cluster config, global config, model registry, and SSH key registry."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

import asyncssh
from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from ..auth import require_token
from ..config import settings
from ..db import get_session
from ..inference import simulation as sim
from ..inference.orchestrator import orchestrator
from ..models import Cluster, KeyRef, ModelDef
from ..schemas import (
    ClusterIn,
    ClusterOut,
    ClusterPatch,
    GlobalConfigIn,
    GlobalConfigOut,
    KeyIn,
    KeyOut,
)
from ..services import topology
from ..ssh import secrets_store
from ..ssh.secrets_store import SecretError

log = logging.getLogger(__name__)

router = APIRouter(tags=["clusters"], dependencies=[Depends(require_token)])


# ------------------------------------------------------------------ clusters
async def _to_out(session: AsyncSession, cl: Cluster) -> ClusterOut:
    edges, clouds = await topology.cluster_devices(session, cl.id)
    return ClusterOut(
        id=cl.id,
        queue_name=cl.ensure_queue_name(),
        model_name=cl.model_name,
        batch_size=cl.batch_size,
        num_bit=cl.num_bit,
        cut_layer=cl.cut_layer,
        edge_devices=[d.id for d in edges],
        cloud_devices=[d.id for d in clouds],
        live=orchestrator.is_live(cl.id),
    )


@router.get("/clusters", response_model=list[ClusterOut])
async def list_clusters(session: AsyncSession = Depends(get_session)) -> list[ClusterOut]:
    # Touch build_clusters first so rows exist for every group the config implies.
    await topology.build_clusters(session)
    rows = (await session.exec(select(Cluster))).all()
    return [await _to_out(session, cl) for cl in sorted(rows, key=lambda c: c.id)]


@router.post("/clusters", response_model=ClusterOut, status_code=status.HTTP_201_CREATED)
async def upsert_cluster(
    payload: ClusterIn, session: AsyncSession = Depends(get_session)
) -> ClusterOut:
    """Create or replace a cluster's config (the Clusters tab saves here)."""
    cfg = await topology.load_global_config(session)
    cl = await session.get(Cluster, payload.id)
    if cl is None:
        cl = Cluster(id=payload.id)

    cl.model_name = payload.model_name or cfg.model_name
    cl.batch_size = payload.batch_size
    cl.num_bit = payload.num_bit
    cl.cut_layer = payload.cut_layer
    cl.queue_name = payload.queue_name or f"intermediate_queue_{payload.id}"
    _validate_cut(cl)

    session.add(cl)
    await session.commit()
    await session.refresh(cl)
    return await _to_out(session, cl)


@router.patch("/clusters/{cluster_id}", response_model=ClusterOut)
async def patch_cluster(
    cluster_id: int, payload: ClusterPatch, session: AsyncSession = Depends(get_session)
) -> ClusterOut:
    cl = await session.get(Cluster, cluster_id)
    if cl is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"cluster {cluster_id} not found")

    data = payload.model_dump(exclude_unset=True)
    # cut_layer is nullable: an explicit null means "back to auto".
    for field, value in data.items():
        if field != "cut_layer" and value is None:
            continue
        setattr(cl, field, value)
    cl.ensure_queue_name()
    _validate_cut(cl)

    session.add(cl)
    await session.commit()
    await session.refresh(cl)
    return await _to_out(session, cl)


def _validate_cut(cl: Cluster) -> None:
    if cl.cut_layer is None:
        return
    layers = sim.get_model(cl.model_name).layers
    if not 1 <= cl.cut_layer <= len(layers) - 1:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"cut_layer must be in 1..{len(layers) - 1} for model {cl.model_name!r}",
        )


# -------------------------------------------------------------------- config
@router.get("/config", response_model=GlobalConfigOut)
async def get_config(session: AsyncSession = Depends(get_session)) -> GlobalConfigOut:
    cfg = await topology.load_global_config(session)
    return GlobalConfigOut(
        clustering=cfg.clustering,
        num_clusters=cfg.num_clusters,
        auto_balance=cfg.auto_balance,
        manual_enabled=cfg.manual_enabled,
        manual_split=cfg.manual_split,
        model_name=cfg.model_name,
        max_message_mb=settings.max_message_mb,
    )


@router.patch("/config", response_model=GlobalConfigOut)
async def patch_config(
    payload: GlobalConfigIn, session: AsyncSession = Depends(get_session)
) -> GlobalConfigOut:
    """Mirror the UI's header/Clusters controls so cut selection matches."""
    cfg = await topology.load_global_config(session)
    for field, value in payload.model_dump(exclude_unset=True, exclude_none=True).items():
        setattr(cfg, field, value)
    cfg.num_clusters = max(1, cfg.num_clusters)
    cfg.manual_split = max(1, cfg.manual_split)
    session.add(cfg)
    await session.commit()
    await session.refresh(cfg)
    return await get_config(session)


# -------------------------------------------------------------------- models
@router.get("/models")
async def list_models(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    extra = await topology.load_custom_models(session)
    out = []
    for name, model in {**sim.BUILTIN_MODELS, **extra}.items():
        cum = sim.prefix(model.layers)
        out.append(
            {
                "value": name,
                "label": model.label,
                "layer_count": len(model.layers),
                "total_gflops": round(cum[-1], 3),
                "builtin": name in sim.BUILTIN_MODELS,
                "layers": [
                    {"name": l.name, "flops": l.flops, "bytes": l.bytes} for l in model.layers
                ],
            }
        )
    return {"models": out, "max_message_mb": settings.max_message_mb}


@router.post("/models", status_code=status.HTTP_201_CREATED)
async def upsert_model(
    payload: dict[str, Any], session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """Register a custom model (the UI's uploaded-model JSON: {name,label,layers})."""
    name = str(payload.get("name") or "uploaded").strip()
    if name in sim.BUILTIN_MODELS:
        raise HTTPException(status.HTTP_409_CONFLICT, f"{name!r} is a built-in model")
    layers = payload.get("layers") or []
    try:
        model = sim.model_from_layers(name, str(payload.get("label") or name), layers)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"invalid layers: {exc}") from exc

    row = await session.get(ModelDef, name) or ModelDef(name=name)
    row.label = model.label
    row.layers = [{"name": l.name, "flops": l.flops, "bytes": l.bytes} for l in model.layers]
    session.add(row)
    await session.commit()
    return {"name": name, "label": model.label, "layer_count": len(model.layers)}


# `response_model=None` is required on 204s: this module uses postponed
# annotations, so FastAPI would otherwise resolve `-> None` to NoneType and
# treat it as a response body.
@router.delete("/models/{name}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_model(name: str, session: AsyncSession = Depends(get_session)) -> None:
    row = await session.get(ModelDef, name)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"model {name!r} not found")
    await session.delete(row)
    await session.commit()


# ---------------------------------------------------------------------- keys
@router.get("/keys", response_model=list[KeyOut])
async def list_keys(session: AsyncSession = Depends(get_session)) -> list[KeyOut]:
    rows = (await session.exec(select(KeyRef))).all()
    return [
        KeyOut(
            id=k.id,
            label=k.label,
            fingerprint=k.fingerprint,
            has_passphrase=k.has_passphrase,
            created_at=k.created_at,
        )
        for k in rows
    ]


@router.post("/keys", response_model=KeyOut, status_code=status.HTTP_201_CREATED)
async def add_key(payload: KeyIn, session: AsyncSession = Depends(get_session)) -> KeyOut:
    """Store an SSH private key on disk under `secrets/<id>.pem`.

    The key material is write-only: no endpoint ever returns it.
    """
    try:
        ref = secrets_store.validate_ref(payload.id)
    except SecretError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    pem = payload.private_key
    if not pem.endswith("\n"):
        pem += "\n"

    # Parse before writing so a malformed key fails loudly at registration.
    try:
        parsed = asyncssh.import_private_key(pem, payload.passphrase or None)
        fingerprint = parsed.get_fingerprint()
    except asyncssh.KeyEncryptionError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "key is encrypted and the passphrase is missing or wrong",
        ) from exc
    except (asyncssh.KeyImportError, ValueError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"unreadable private key: {exc}") from exc

    secrets_store.save_key(ref, pem)
    if payload.passphrase:
        secrets_store.set_passphrase(ref, payload.passphrase)

    row = await session.get(KeyRef, ref) or KeyRef(id=ref)
    row.label = payload.label or ref
    row.fingerprint = fingerprint or hashlib.sha256(pem.encode()).hexdigest()[:32]
    row.has_passphrase = bool(payload.passphrase)
    session.add(row)
    await session.commit()
    await session.refresh(row)

    return KeyOut(
        id=row.id,
        label=row.label,
        fingerprint=row.fingerprint,
        has_passphrase=row.has_passphrase,
        created_at=row.created_at,
    )


@router.delete("/keys/{key_ref}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_key(key_ref: str, session: AsyncSession = Depends(get_session)) -> None:
    row = await session.get(KeyRef, key_ref)
    try:
        removed = secrets_store.delete_key(key_ref)
    except SecretError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    if row is None and not removed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"key {key_ref!r} not found")
    secrets_store.set_passphrase(key_ref, "")
    if row is not None:
        await session.delete(row)
        await session.commit()
