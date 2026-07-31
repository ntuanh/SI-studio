"""Broker / control-API server config and connection test.

Backs the Control tab's top card (IP / port / user / password + Test
connection). The password is accepted on write only: it goes straight into the
encrypted secret store and the row keeps just a `password_ref`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel.ext.asyncio.session import AsyncSession

from ..auth import require_token
from ..config import settings
from ..db import get_session
from ..inference.broker import probe_broker
from ..models import ServerConfig
from ..schemas import ServerConfigIn, ServerConfigOut, ServerTestOut
from ..services.metrics_bus import bus
from ..services.server_state import server_state
from ..ssh import gateway, secrets_store
from ..ssh.commands import run_command
from ..ssh.pool import SSHError, pool

log = logging.getLogger(__name__)

router = APIRouter(prefix="/server", tags=["server"], dependencies=[Depends(require_token)])

API_PROBE_TIMEOUT = 5.0


# ------------------------------------------------------------------- helpers
async def load_server_config(session: AsyncSession) -> ServerConfig:
    """Fetch the singleton row, creating the default on first use."""
    cfg = await session.get(ServerConfig, 1)
    if cfg is None:
        cfg = ServerConfig(id=1)
        session.add(cfg)
        await session.commit()
        await session.refresh(cfg)
    return cfg


def _settings_broker_host() -> str:
    """The host inside `BROKER_URL` -- the broker this backend actually uses."""
    try:
        return urlsplit(settings.broker_url).hostname or ""
    except ValueError:
        return ""


def resolved_amqp_host(cfg: ServerConfig) -> str:
    """Where RabbitMQ is.

    An explicit `amqp_host` wins. Otherwise fall back to `BROKER_URL`, which is
    the broker this process is already connected to -- a far better guess than
    the SSH gateway, which frequently runs no broker at all.
    """
    return (cfg.amqp_host or "").strip() or _settings_broker_host()


def _amqp_url(cfg: ServerConfig) -> str:
    """Build the connection URL, resolving the password from the secret store.

    Never logged or returned -- only handed to `probe_broker`.
    """
    password = (
        secrets_store.get_secret(cfg.password_ref, "password") if cfg.password_ref else None
    ) or ""
    user = quote(cfg.username or "guest", safe="")
    secret = quote(password, safe="")
    return f"amqp://{user}:{secret}@{resolved_amqp_host(cfg)}:{cfg.port}/"


def _sync_state(cfg: ServerConfig) -> None:
    """Mirror the row into the process cache used for `$BROKER_IP`.

    `$BROKER_IP` is substituted into `iperf3 -c $BROKER_IP` and
    `ping -c 3 $BROKER_IP`, which run **on the devices** to measure their link
    to the broker -- so it is the AMQP host, never the SSH gateway.
    """
    server_state.update(
        host=resolved_amqp_host(cfg),
        port=cfg.port,
        api_port=cfg.api_port,
        username=cfg.username,
        has_credentials=bool(cfg.password_ref)
        and secrets_store.has_secret(cfg.password_ref, "password"),
    )


def _to_out(cfg: ServerConfig, *, auth_ok: bool = False) -> ServerConfigOut:
    has_creds = bool(cfg.password_ref) and secrets_store.has_secret(
        cfg.password_ref, "password"
    )
    return ServerConfigOut(
        host=cfg.host,
        port=cfg.port,
        api_port=cfg.api_port,
        username=cfg.username,
        has_credentials=has_creds,
        ip=cfg.host,
        user=cfg.username,
        status=server_state.status,
        auth_ok=auth_ok,
        updated_at=cfg.updated_at,
        amqp_host=cfg.amqp_host,
        # What the broker probe and `$BROKER_IP` will actually use, so the UI
        # can show the fallback rather than an empty box.
        amqp_host_resolved=resolved_amqp_host(cfg),
        ssh_port=cfg.ssh_port,
        ssh_username=cfg.ssh_username,
        ssh_user=cfg.ssh_username,
        has_ssh_credentials=bool(cfg.ssh_password_ref)
        and secrets_store.has_secret(cfg.ssh_password_ref, "password"),
        ssh_status=pool.status(gateway.SERVER_DEVICE_ID),
        jump_enabled=cfg.jump_enabled,
    )


async def refresh_server_state(session: AsyncSession) -> None:
    """Called on startup so `$BROKER_IP` works before any config POST."""
    _sync_state(await load_server_config(session))


# ----------------------------------------------------------------- endpoints
@router.get("/config", response_model=ServerConfigOut)
async def get_config(session: AsyncSession = Depends(get_session)) -> ServerConfigOut:
    """Current config **without** the password (only `has_credentials`)."""
    cfg = await load_server_config(session)
    _sync_state(cfg)
    return _to_out(cfg)


@router.post("/config", response_model=ServerConfigOut)
async def set_config(
    payload: ServerConfigIn, session: AsyncSession = Depends(get_session)
) -> ServerConfigOut:
    """Upsert the singleton. A supplied password is encrypted into the secret
    store and never echoed back."""
    cfg = await load_server_config(session)

    host_changed = cfg.host != payload.host

    cfg.host = payload.host
    cfg.port = payload.port
    if payload.api_port is not None:
        cfg.api_port = payload.api_port
    cfg.username = payload.username
    cfg.updated_at = datetime.now(timezone.utc)

    if payload.password is not None:
        ref = cfg.password_ref or ServerConfig.SECRET_REF
        secrets_store.set_secret(ref, "password", payload.password)
        # An empty password clears the slot; drop the dangling ref with it.
        cfg.password_ref = ref if payload.password else None

    if payload.amqp_host is not None:
        cfg.amqp_host = payload.amqp_host.strip()

    # --- SSH leg: only touch what was actually sent ---
    ssh_changed = host_changed
    if payload.ssh_port is not None and payload.ssh_port != cfg.ssh_port:
        cfg.ssh_port, ssh_changed = payload.ssh_port, True
    if payload.ssh_username is not None and payload.ssh_username != cfg.ssh_username:
        cfg.ssh_username, ssh_changed = payload.ssh_username.strip(), True
    if payload.jump_enabled is not None:
        cfg.jump_enabled = payload.jump_enabled
    if payload.ssh_password is not None:
        # Filed under the reserved target id so the pool resolves it with no
        # special case -- the server borrows the device credential path, and
        # the ref must therefore be that same id.
        secrets_store.set_password(gateway.SERVER_DEVICE_ID, payload.ssh_password)
        cfg.ssh_password_ref = gateway.SERVER_DEVICE_ID if payload.ssh_password else None
        ssh_changed = True

    session.add(cfg)
    await session.commit()
    await session.refresh(cfg)

    # A changed target invalidates the previous test result, and any session
    # open to the old host is now pointing at the wrong machine.
    server_state.status = "off"
    if ssh_changed:
        await pool.disconnect(gateway.SERVER_DEVICE_ID)
    _sync_state(cfg)
    bus.event("server_config_updated", host=cfg.host, port=cfg.port, api_port=cfg.api_port)

    return _to_out(cfg, auth_ok=False)


@router.post("/ssh/test")
async def test_ssh(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Log into the server and run `uname -a`. SSH only -- no broker, no API.

    Separate from `/server/test` because they answer different questions.
    Getting a shell on a machine has nothing to do with whether RabbitMQ is
    running, and making one wait on the other means a red dot for a server you
    are perfectly able to log into.
    """
    cfg = await load_server_config(session)
    if not gateway.is_configured(cfg):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "set the server's IP and SSH user first",
        )

    result = await _probe_ssh(cfg)
    ok = result["state"] == "ok"
    bus.server_status("on" if ok else "error", host=cfg.host, ssh=result["state"])

    return {
        "ok": ok,
        "host": cfg.host,
        "port": cfg.ssh_port,
        "user": cfg.ssh_username,
        "banner": result.get("banner", ""),
        "error": result.get("error", ""),
        "checked_at": datetime.now(timezone.utc),
    }


@router.post("/test", response_model=ServerTestOut)
async def test_connection(session: AsyncSession = Depends(get_session)) -> ServerTestOut:
    """Check all three legs of the server: SSH login, AMQP handshake, API health.

    The SSH leg logs in and runs `uname -a`, so a pass proves the credentials
    work *and* that commands execute -- not merely that port 22 accepted a TCP
    connection. Broadcasts `server_status` so the UI dot updates without
    polling.

    `ok` needs every *configured* leg to pass. With no SSH username on file the
    SSH leg reports `skipped` and does not drag the result down; the card is
    still usable as broker-only config, which is what it was before.
    """
    cfg = await load_server_config(session)
    if not cfg.host:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "no server host configured; POST /server/config first"
        )

    _sync_state(cfg)

    broker_result, api_result, ssh_result = await asyncio.gather(
        probe_broker(_amqp_url(cfg)),
        _probe_api(cfg.api_base_url),
        _probe_ssh(cfg),
    )

    api_up = bool(api_result.get("ok"))
    ssh_state = ssh_result["state"]
    ok = bool(broker_result.get("ok")) and api_up and ssh_state != "failed"
    server_state.status = "on" if ok else "error"

    bus.server_status(
        server_state.status,
        host=cfg.host,
        rabbitmq_version=broker_result.get("rabbitmq_version", ""),
        api="up" if api_up else "down",
        ssh=ssh_state,
        error=broker_result.get("error")
        or api_result.get("error")
        or ssh_result.get("error")
        or "",
    )

    log.info(
        "server test host=%s ssh=%s amqp=%s api=%s",
        cfg.host,
        ssh_state if ssh_state != "failed" else ssh_result.get("error"),
        "ok" if broker_result.get("ok") else broker_result.get("error"),
        "up" if api_up else api_result.get("error"),
    )

    return ServerTestOut(
        ok=ok,
        rabbitmq_version=broker_result.get("rabbitmq_version", ""),
        product=broker_result.get("product", ""),
        cluster_name=broker_result.get("cluster_name", ""),
        platform=broker_result.get("platform", ""),
        api="up" if api_up else "down",
        api_detail=api_result.get("detail", ""),
        broker_error=broker_result.get("error", ""),
        host=cfg.host,
        checked_at=datetime.now(timezone.utc),
        ssh=ssh_state,
        ssh_error=ssh_result.get("error", ""),
        ssh_banner=ssh_result.get("banner", ""),
    )


# -------------------------------------------------------------------- probes
async def _probe_ssh(cfg: ServerConfig) -> dict[str, Any]:
    """Log in and run `uname -a`. Leaves the session open for reuse.

    Not closed afterwards on purpose: a successful test is almost always
    followed by commands or by devices tunnelling through, and the pool exists
    to avoid re-dialling.
    """
    if not gateway.is_configured(cfg):
        return {"state": "skipped", "error": "", "banner": ""}

    def clean(message: str) -> str:
        # The pool prefixes errors with the target id; `__server__:` is an
        # internal name and means nothing to whoever is reading the card.
        return message.removeprefix(f"{gateway.SERVER_DEVICE_ID}: ").strip()

    try:
        conn = await pool.get(gateway.server_device(cfg))
        result = await run_command(conn, "uname -a", timeout=15)
    except SSHError as exc:
        return {"state": "failed", "error": clean(str(exc)), "banner": ""}
    except Exception as exc:  # noqa: BLE001 - report, never 500 the card
        return {"state": "failed", "error": f"{type(exc).__name__}: {exc}", "banner": ""}

    if not result.ok:
        # Windows and some appliances have no `uname`; the login still worked,
        # which is what the test is really asking about.
        detail = (result.stderr or result.error or "").strip()
        return {"state": "ok", "error": "", "banner": f"(login ok; uname unavailable: {detail[:80]})"}

    return {"state": "ok", "error": "", "banner": (result.stdout or "").strip()[:200]}


async def _probe_api(base_url: str) -> dict[str, Any]:
    """GET <base>/health without pulling in an HTTP client dependency."""
    url = f"{base_url.rstrip('/')}/health"

    def fetch() -> dict[str, Any]:
        req = Request(url, headers={"Accept": "application/json"})
        try:
            with urlopen(req, timeout=API_PROBE_TIMEOUT) as resp:  # noqa: S310 - http scheme from config
                body = resp.read().decode("utf-8", "replace")
                detail = ""
                try:
                    parsed = json.loads(body)
                    if isinstance(parsed, dict):
                        detail = str(parsed.get("status", ""))
                except json.JSONDecodeError:
                    detail = body[:120]
                return {"ok": 200 <= resp.status < 300, "detail": detail, "error": ""}
        except HTTPError as exc:
            # It answered, so something is listening -- just not healthily.
            return {"ok": False, "detail": f"HTTP {exc.code}", "error": f"HTTP {exc.code}"}
        except (URLError, OSError) as exc:
            return {"ok": False, "detail": "", "error": f"{type(exc).__name__}: {exc}"}

    return await asyncio.to_thread(fetch)
