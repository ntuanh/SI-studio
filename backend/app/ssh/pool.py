"""AsyncSSH connection pool, one reused connection per device (guide §5).

Every status transition (off | connecting | on | error) is published to the
metrics bus so the UI's per-device dots update live.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Literal

import asyncssh

from ..config import settings
from ..models import Device
from ..services.metrics_bus import bus
from . import secrets_store

log = logging.getLogger(__name__)

Status = Literal["off", "connecting", "on", "error"]

#: Returns the connection to tunnel through, or None to dial directly.
JumpProvider = Callable[[], Awaitable["asyncssh.SSHClientConnection | None"]]


class SSHError(RuntimeError):
    """Connection could not be established / is not open."""


def _target_of(device: Device) -> tuple[str, int, str]:
    """What a connection for `device` is actually dialled against."""
    return (device.host or "", device.port or 22, device.username or "root")


def _fmt_target(target: tuple[str, int, str] | None) -> str:
    if not target:
        return "unknown"
    host, port, user = target
    return f"{user}@{host}:{port}"


class SSHPool:
    """Holds `{device_id: SSHClientConnection}` and keeps the UI informed."""

    def __init__(self) -> None:
        self._conns: dict[str, asyncssh.SSHClientConnection] = {}
        #: What each open connection was actually dialled with. A cached
        #: connection is only valid while the device still points at the same
        #: place -- see `_target_of`.
        self._targets: dict[str, tuple[str, int, str]] = {}
        self._status: dict[str, Status] = {}
        #: Per-device lock so two concurrent callers don't dial the same host twice.
        self._locks: dict[str, asyncio.Lock] = {}
        self._sem = asyncio.Semaphore(max(1, settings.fanout_concurrency))
        #: Installed at startup; returns the jump host's connection or None.
        #: A callback rather than an import so `ssh/` stays free of DB/router
        #: dependencies and this class remains unit-testable on its own.
        self._jump_provider: JumpProvider | None = None

    def set_jump_provider(self, provider: JumpProvider | None) -> None:
        self._jump_provider = provider

    # ------------------------------------------------------------ bookkeeping
    def _lock(self, device_id: str) -> asyncio.Lock:
        return self._locks.setdefault(device_id, asyncio.Lock())

    def _set_status(self, device_id: str, status: Status, detail: str = "") -> None:
        if self._status.get(device_id) == status and not detail:
            return
        self._status[device_id] = status
        bus.ssh_status(device_id, status, detail)

    def status(self, device_id: str) -> Status:
        return self._status.get(device_id, "off")

    def statuses(self) -> dict[str, Status]:
        return dict(self._status)

    def is_open(self, device_id: str) -> bool:
        conn = self._conns.get(device_id)
        return conn is not None and not conn.is_closed()

    @property
    def semaphore(self) -> asyncio.Semaphore:
        return self._sem

    # -------------------------------------------------------------- connecting
    def _connect_kwargs(self, device: Device, password_override: str | None) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "host": device.host,
            "port": device.port or 22,
            "username": device.username or "root",
            # Devices are lab machines whose host keys churn; the UI has no
            # place to accept a fingerprint, so verification is off by design.
            "known_hosts": None,
            "connect_timeout": settings.ssh_connect_timeout,
            "keepalive_interval": 30,
        }

        password = password_override or secrets_store.get_password(device.id)

        if device.auth_method == "password":
            if not password:
                raise SSHError(f"{device.id}: auth_method=password but no password on file")
            kwargs["password"] = password
            kwargs["client_keys"] = None  # don't let agent keys shadow the password
        else:
            if not device.key_ref:
                raise SSHError(f"{device.id}: auth_method=key but key_ref is empty")
            path = secrets_store.key_path(device.key_ref)
            if not path.is_file():
                raise SSHError(f"{device.id}: private key not found for key_ref={device.key_ref!r}")
            kwargs["client_keys"] = [str(path)]
            passphrase = secrets_store.get_passphrase(device.key_ref)
            if passphrase:
                kwargs["passphrase"] = passphrase

        return kwargs

    async def _tunnel_for(self, device: Device) -> asyncssh.SSHClientConnection | None:
        """The jump connection for `device`, or None to dial it directly.

        The server never tunnels through itself -- that would deadlock on the
        per-target lock, since opening the jump host is what we are inside of.
        """
        from .gateway import is_server

        if self._jump_provider is None or is_server(device.id):
            return None
        try:
            return await self._jump_provider()
        except SSHError as exc:
            # Surface it against the device being dialled: from the operator's
            # side, "cannot reach dA" is the symptom and the jump host is the
            # cause, so say both.
            raise SSHError(f"{device.id}: jump host unreachable ({exc})") from exc

    async def get(
        self, device: Device, *, password_override: str | None = None
    ) -> asyncssh.SSHClientConnection:
        """Return an open connection, reusing one if it still points at this device.

        Reuse is keyed on the *target*, not just the device id. Editing a
        device's host or username and reconnecting used to hand back the
        session opened against the previous machine, so commands ran on the
        wrong box with no indication anything was amiss -- the id matched, so
        the pool was satisfied.
        """
        async with self._lock(device.id):
            conn = self._conns.get(device.id)
            target = _target_of(device)
            if conn is not None and not conn.is_closed():
                if self._targets.get(device.id) == target:
                    self._set_status(device.id, "on")
                    return conn
                old = self._targets.get(device.id)
                log.info(
                    "ssh target changed for %s (%s -> %s); reconnecting",
                    device.id, _fmt_target(old), _fmt_target(target),
                )
                bus.exec_line(
                    device.id,
                    f"⇄ {device.name}: target changed "
                    f"({_fmt_target(old)} → {_fmt_target(target)}), reconnecting",
                    "meta",
                )
                conn.close()

            self._conns.pop(device.id, None)
            self._targets.pop(device.id, None)
            if not device.host:
                self._set_status(device.id, "error", "no host configured")
                raise SSHError(f"{device.id}: no host configured")

            self._set_status(device.id, "connecting")
            try:
                kwargs = self._connect_kwargs(device, password_override)
                tunnel = await self._tunnel_for(device)
                if tunnel is not None:
                    # asyncssh opens the session over a channel on `tunnel`,
                    # i.e. ProxyJump. `host` is then resolved by the jump host,
                    # so a private address the control plane cannot route to is
                    # exactly the point.
                    kwargs["tunnel"] = tunnel
                conn = await asyncio.wait_for(
                    asyncssh.connect(**kwargs),
                    # The jump hop has its own dial inside this budget.
                    timeout=settings.ssh_connect_timeout + 5,
                )
            except SSHError as exc:
                self._set_status(device.id, "error", str(exc))
                raise
            except asyncio.TimeoutError as exc:
                msg = f"timeout after {settings.ssh_connect_timeout}s"
                self._set_status(device.id, "error", msg)
                raise SSHError(f"{device.id}: {msg}") from exc
            except (OSError, asyncssh.Error) as exc:
                self._set_status(device.id, "error", str(exc))
                raise SSHError(f"{device.id}: {exc}") from exc

            self._conns[device.id] = conn
            self._targets[device.id] = _target_of(device)
            self._set_status(device.id, "on")
            log.info("ssh open %s -> %s@%s:%s", device.id, device.username, device.host, device.port)
            return conn

    async def connect_all(
        self, devices: list[Device], *, passwords: dict[str, str] | None = None
    ) -> dict[str, dict[str, Any]]:
        """Open sessions to every device concurrently (bounded)."""
        passwords = passwords or {}

        async def one(d: Device) -> tuple[str, dict[str, Any]]:
            async with self._sem:
                try:
                    await self.get(d, password_override=passwords.get(d.id))
                    return d.id, {"status": "on", "detail": ""}
                except SSHError as exc:
                    return d.id, {"status": "error", "detail": str(exc)}

        results = await asyncio.gather(*(one(d) for d in devices))
        return dict(results)

    # ----------------------------------------------------------- disconnecting
    async def disconnect(self, device_id: str) -> bool:
        async with self._lock(device_id):
            conn = self._conns.pop(device_id, None)
            self._targets.pop(device_id, None)
            if conn is None:
                self._set_status(device_id, "off")
                return False
            conn.close()
            try:
                await asyncio.wait_for(conn.wait_closed(), timeout=5)
            except asyncio.TimeoutError:
                log.warning("ssh close timed out for %s", device_id)
            self._set_status(device_id, "off")
            return True

    async def disconnect_all(self, device_ids: list[str] | None = None) -> dict[str, bool]:
        ids = list(device_ids if device_ids is not None else self._conns.keys())
        results = await asyncio.gather(*(self.disconnect(i) for i in ids))
        return dict(zip(ids, results))

    async def aclose(self) -> None:
        await self.disconnect_all()


#: Process-wide singleton, wired into the app lifespan.
pool = SSHPool()
