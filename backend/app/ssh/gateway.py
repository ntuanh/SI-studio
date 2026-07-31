"""The control server as an SSH target, and as a jump host for the devices.

Two jobs, both driven by the Control tab's top card:

1. **Command it directly.** The server is addressable as a normal target under
   the reserved id `__server__`, so `/control/connect`, `/control/exec`, and
   `/control/scp` work on it with no special-casing beyond resolution. It is
   deliberately *not* a row in the `device` table: it has no GFLOPS and no
   cluster, and a phantom device would land in `build_clusters()` and skew
   every metric the simulator produces.

2. **Reach the devices through it.** With `jump_enabled`, every device
   connection is opened over a channel on the server's own connection --
   asyncssh's `tunnel=`, which is OpenSSH's `ProxyJump`. That is the usual lab
   shape: one routable machine, and the edge/cloud nodes on a private network
   behind it.

The pool asks this module for the jump connection through a callback installed
at startup, so `ssh/` never imports a router and the dependency stays one-way.
"""

from __future__ import annotations

import logging

import asyncssh

from ..models import Device, ServerConfig

log = logging.getLogger(__name__)

#: Reserved target id. Not a valid device id -- ids are minted as `d<hex>`.
SERVER_DEVICE_ID = "__server__"
SERVER_DEVICE_NAME = "Control server"

#: The pool resolves a target's password by target id, so the server's SSH
#: password must be filed under exactly this id. Checked here rather than
#: trusted, because the failure it prevents is silent: the password would be
#: stored successfully and simply never found again.
assert ServerConfig.SSH_SECRET_REF == SERVER_DEVICE_ID, (
    "ServerConfig.SSH_SECRET_REF must match gateway.SERVER_DEVICE_ID"
)


def is_server(device_id: str) -> bool:
    return device_id == SERVER_DEVICE_ID


def server_device(cfg: ServerConfig) -> Device:
    """A transient `Device` describing the server's SSH login.

    Never added to a session -- it exists only to satisfy the pool and the
    command layer, which are written against `Device`. Its password lives in
    the secret store under the same reserved id, so `secrets_store` needs no
    special case either.
    """
    return Device(
        id=SERVER_DEVICE_ID,
        name=SERVER_DEVICE_NAME,
        kind="Custom",
        cluster_id=0,
        host=cfg.host,
        port=cfg.ssh_port or 22,
        username=cfg.ssh_username or "root",
        # Always password auth: the card offers no key_ref field, so falling
        # back to "key" when no password is on file only produces the baffling
        # "auth_method=key but key_ref is empty" instead of the true reason.
        auth_method="password",
        key_ref="",
        gflops=0.0,
        bandwidth_mb_s=0.0,
        latency_ms=0.0,
        stage_name="Server",
    )


def device_amqp_url(cfg: ServerConfig | None) -> str:
    """The broker URL **as a device should dial it**, password included.

    Lives here rather than in `routers/server.py` because the measurement
    service needs it too, and a service importing a router would invert the
    dependency this module exists to keep one-way.

    Returns "" when there is nothing usable -- no host, or a host that means
    "this machine" on the device rather than the broker. Never logged.
    """
    from urllib.parse import quote, urlsplit

    from ..config import settings
    from . import secrets_store

    if cfg is None:
        host, port = "", 5672
        user, password = "", ""
    else:
        host = (cfg.amqp_host or "").strip()
        port = cfg.port or 5672
        user = cfg.username or "guest"
        password = (
            secrets_store.get_secret(cfg.password_ref, "password")
            if cfg.password_ref
            else ""
        ) or ""

    if not host:
        # Fall back to whatever the agents are configured with.
        try:
            parsed = urlsplit(settings.agent_broker_url)
        except ValueError:
            return ""
        host = parsed.hostname or ""
        port = parsed.port or port
        user = parsed.username or user or "guest"
        password = parsed.password or password

    from .commands import LOOPBACK_HOSTS

    if not host or host.lower() in LOOPBACK_HOSTS:
        # On a device, "localhost" is the device. A benchmark against it would
        # report a fictional link rather than fail, which is worse.
        return ""

    return (
        f"amqp://{quote(user or 'guest', safe='')}:"
        f"{quote(password, safe='')}@{host}:{port}/"
    )


def is_configured(cfg: ServerConfig) -> bool:
    return bool(cfg.host and cfg.ssh_username)


async def load_config() -> ServerConfig | None:
    """Read the singleton on its own session.

    The pool is called from request handlers, background tasks, and the
    orchestrator alike, so it cannot borrow a caller's session without
    inheriting that caller's transaction.
    """
    from ..db import SessionFactory

    try:
        async with SessionFactory() as session:
            return await session.get(ServerConfig, 1)
    except Exception:  # noqa: BLE001 - a DB hiccup must not break dialling
        log.exception("could not load server config; jump host disabled for this attempt")
        return None


async def jump_connection() -> asyncssh.SSHClientConnection | None:
    """The connection device dials should tunnel through, or None.

    Returns None -- meaning "connect directly" -- when no server is configured
    or jumping is switched off. A configured-but-unreachable server raises, so
    the device's own status carries the real reason instead of silently
    falling back to a direct dial that will also fail, more confusingly.
    """
    from .pool import pool  # local: pool imports this module's callback

    cfg = await load_config()
    if cfg is None or not cfg.jump_enabled or not is_configured(cfg):
        return None
    return await pool.get(server_device(cfg))
