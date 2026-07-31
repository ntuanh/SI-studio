"""Turn the DB inventory into the cluster shape the simulator expects.

This is the backend port of the UI's `buildClusters()` + `clusterConfig()`.
"""

from __future__ import annotations

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from ..inference import simulation as sim
from ..models import Cluster, Device, GlobalConfig, ModelDef


async def load_global_config(session: AsyncSession) -> GlobalConfig:
    """Fetch the single config row, creating the default on first use."""
    cfg = await session.get(GlobalConfig, 1)
    if cfg is None:
        cfg = GlobalConfig(id=1)
        session.add(cfg)
        await session.commit()
        await session.refresh(cfg)
    return cfg


async def load_custom_models(session: AsyncSession) -> dict[str, sim.Model]:
    """User-uploaded model definitions, keyed by name."""
    rows = (await session.exec(select(ModelDef))).all()
    out: dict[str, sim.Model] = {}
    for row in rows:
        try:
            out[row.name] = sim.model_from_layers(row.name, row.label, row.layers or [])
        except ValueError:
            continue
    return out


async def ensure_cluster(session: AsyncSession, cluster_id: int, *, model_name: str) -> Cluster:
    """Get-or-create a cluster row with the UI's defaults."""
    cl = await session.get(Cluster, cluster_id)
    if cl is None:
        cl = Cluster(id=cluster_id, model_name=model_name)
        cl.ensure_queue_name()
        session.add(cl)
        await session.commit()
        await session.refresh(cl)
    elif not cl.queue_name:
        cl.ensure_queue_name()
        session.add(cl)
        await session.commit()
    return cl


def _spec(d: Device) -> sim.DeviceSpec:
    return sim.DeviceSpec(
        id=d.id,
        name=d.name,
        side=d.side,
        gflops=float(d.gflops or 0.0),
        bandwidth_mb_s=float(d.bandwidth_mb_s or 0.0),
        latency_ms=float(d.latency_ms or 0.0),
    )


async def build_clusters(session: AsyncSession) -> list[sim.ClusterInput]:
    """Group every device into clusters exactly like the UI does.

    With clustering on, a device's `cluster_id` is clamped into [1, num_clusters]
    and empty groups are still returned (they simulate as idle). With clustering
    off, everything collapses into cluster 1.
    """
    cfg = await load_global_config(session)
    devices = (await session.exec(select(Device))).all()

    if cfg.clustering:
        k_max = max(1, cfg.num_clusters)
        groups: dict[int, tuple[list[sim.DeviceSpec], list[sim.DeviceSpec]]] = {
            k: ([], []) for k in range(1, k_max + 1)
        }
        for d in devices:
            k = min(k_max, max(1, int(round(d.cluster_id or 1))))
            edges, clouds = groups[k]
            (clouds if d.side == "cloud" else edges).append(_spec(d))
    else:
        groups = {
            1: (
                [_spec(d) for d in devices if d.side != "cloud"],
                [_spec(d) for d in devices if d.side == "cloud"],
            )
        }

    out: list[sim.ClusterInput] = []
    for cid in sorted(groups):
        edges, clouds = groups[cid]
        row = await ensure_cluster(session, cid, model_name=cfg.model_name)
        out.append(
            sim.ClusterInput(
                id=cid,
                queue_name=row.queue_name or f"intermediate_queue_{cid}",
                model_name=row.model_name or cfg.model_name,
                num_bit=row.num_bit or 8,
                batch_size=row.batch_size or 32,
                edges=edges,
                clouds=clouds,
                split_override=row.cut_layer,
            )
        )
    return out


async def cluster_devices(session: AsyncSession, cluster_id: int) -> tuple[list[Device], list[Device]]:
    """The real Device rows for one cluster, split into (edges, clouds).

    Respects the same clamping rule as `build_clusters` so a device pinned to
    cluster 7 with num_clusters=2 lands in cluster 2 here too.
    """
    cfg = await load_global_config(session)
    devices = (await session.exec(select(Device))).all()

    edges: list[Device] = []
    clouds: list[Device] = []
    for d in devices:
        if cfg.clustering:
            k_max = max(1, cfg.num_clusters)
            k = min(k_max, max(1, int(round(d.cluster_id or 1))))
        else:
            k = 1
        if k != cluster_id:
            continue
        (clouds if d.side == "cloud" else edges).append(d)
    return edges, clouds


async def simulate_all(session: AsyncSession) -> list[sim.ClusterMetrics | None]:
    """Run the simulator over the whole inventory (used by /metrics/latest)."""
    cfg = await load_global_config(session)
    extra = await load_custom_models(session)
    return [
        sim.sim_cluster(
            cl,
            auto_balance=cfg.auto_balance,
            manual_enabled=cfg.manual_enabled,
            manual_split=cfg.manual_split,
            extra_models=extra,
        )
        for cl in await build_clusters(session)
    ]


async def simulate_cluster(session: AsyncSession, cluster_id: int) -> sim.ClusterMetrics | None:
    cfg = await load_global_config(session)
    extra = await load_custom_models(session)
    for cl in await build_clusters(session):
        if cl.id == cluster_id:
            return sim.sim_cluster(
                cl,
                auto_balance=cfg.auto_balance,
                manual_enabled=cfg.manual_enabled,
                manual_split=cfg.manual_split,
                extra_models=extra,
            )
    return None
