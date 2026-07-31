"""The control server as an SSH target, and as a jump host for the devices."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.models import Device, ServerConfig
from app.ssh import gateway
from app.ssh.pool import SSHError, SSHPool


# ----------------------------------------------------------------- identity
def test_secret_ref_matches_the_target_id() -> None:
    """The pool looks a password up by target id, so a mismatch would store
    the server's password somewhere it can never be found again."""
    assert ServerConfig.SSH_SECRET_REF == gateway.SERVER_DEVICE_ID


def test_server_device_is_never_a_row() -> None:
    cfg = ServerConfig(id=1, host="10.0.0.9", ssh_port=2222, ssh_username="dai")
    d = gateway.server_device(cfg)

    assert d.id == gateway.SERVER_DEVICE_ID
    assert (d.host, d.port, d.username) == ("10.0.0.9", 2222, "dai")
    # No compute and no cluster: if this ever became a device row it would be
    # picked up by build_clusters() and skew every metric.
    assert d.gflops == 0.0
    assert d.cluster_id == 0


def test_is_configured_needs_both_host_and_user() -> None:
    assert not gateway.is_configured(ServerConfig(id=1))
    assert not gateway.is_configured(ServerConfig(id=1, host="10.0.0.9"))
    assert not gateway.is_configured(ServerConfig(id=1, ssh_username="dai"))
    assert gateway.is_configured(ServerConfig(id=1, host="10.0.0.9", ssh_username="dai"))


# --------------------------------------------------------------- jump host
def _device(did: str = "d1") -> Device:
    return Device(id=did, name=did, kind="Edge", cluster_id=1, host="10.0.1.5",
                  username="root", auth_method="password")


@pytest.mark.asyncio
async def test_device_dials_through_the_jump_connection() -> None:
    pool = SSHPool()
    jump = object()
    pool.set_jump_provider(AsyncMock(return_value=jump))

    with patch("asyncssh.connect", new=AsyncMock()) as connect, patch(
        "app.ssh.secrets_store.get_password", return_value="pw"
    ):
        await pool.get(_device())

    assert connect.await_args.kwargs["tunnel"] is jump
    assert connect.await_args.kwargs["host"] == "10.0.1.5"


@pytest.mark.asyncio
async def test_no_provider_means_a_direct_dial() -> None:
    pool = SSHPool()

    with patch("asyncssh.connect", new=AsyncMock()) as connect, patch(
        "app.ssh.secrets_store.get_password", return_value="pw"
    ):
        await pool.get(_device())

    assert "tunnel" not in connect.await_args.kwargs


@pytest.mark.asyncio
async def test_the_server_never_tunnels_through_itself() -> None:
    """Otherwise opening the jump host would recurse into opening the jump
    host, and deadlock on its own per-target lock."""
    pool = SSHPool()
    provider = AsyncMock(return_value=object())
    pool.set_jump_provider(provider)

    cfg = ServerConfig(id=1, host="10.0.0.9", ssh_username="dai")
    with patch("asyncssh.connect", new=AsyncMock()) as connect, patch(
        "app.ssh.secrets_store.get_password", return_value="pw"
    ):
        await pool.get(gateway.server_device(cfg))

    assert "tunnel" not in connect.await_args.kwargs
    provider.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_unreachable_jump_host_names_both_ends() -> None:
    pool = SSHPool()
    pool.set_jump_provider(AsyncMock(side_effect=SSHError("__server__: Permission denied")))

    with patch("app.ssh.secrets_store.get_password", return_value="pw"):
        with pytest.raises(SSHError) as excinfo:
            await pool.get(_device("dA"))

    message = str(excinfo.value)
    assert "dA" in message and "jump host" in message


# ------------------------------------------------------------------- routes
def test_server_config_round_trips_the_ssh_leg(client, auth) -> None:
    body = {
        "ip": "10.0.0.9", "ssh_port": 2222, "ssh_user": "dai", "ssh_password": "s3cret",
        "jump_enabled": True, "port": 5672, "user": "guest", "password": "guest",
    }
    out = client.post("/server/config", json=body, headers=auth).json()

    assert out["ssh_port"] == 2222
    assert out["ssh_user"] == "dai"
    assert out["has_ssh_credentials"] is True
    assert out["jump_enabled"] is True
    # AMQP identity is separate and untouched by the SSH fields.
    assert out["port"] == 5672
    assert out["user"] == "guest"

    # Neither password is ever echoed, on write or on read.
    assert "s3cret" not in out.values()
    read = client.get("/server/config", headers=auth).json()
    assert "ssh_password" not in read
    assert "s3cret" not in str(read)


def test_exec_on_the_server_needs_it_configured(client, auth) -> None:
    client.post("/server/config", json={"ip": "10.0.0.9"}, headers=auth)  # no ssh_user

    r = client.post("/control/exec",
                    json={"device_ids": [gateway.SERVER_DEVICE_ID], "command": "uptime"},
                    headers=auth)
    assert r.status_code == 400
    assert "ssh" in r.json()["detail"].lower()


def test_the_server_is_addressable_but_is_not_a_device(client, auth, ui_export) -> None:
    client.post("/seed", json=ui_export, headers=auth)
    client.post(
        "/server/config",
        json={"ip": "10.0.0.9", "ssh_user": "dai", "ssh_password": "pw"},
        headers=auth,
    )

    listed = [d["id"] for d in client.get("/devices", headers=auth).json()]
    assert gateway.SERVER_DEVICE_ID not in listed

    # ...and it stays out of the cluster maths.
    for payload in client.get("/metrics/latest", headers=auth).json()["clusters"]:
        ids = [d["id"] for d in payload.get("devices", [])]
        assert gateway.SERVER_DEVICE_ID not in ids

    # But it resolves as a target: this reaches the SSH layer and fails to
    # connect (no such host in the test env) rather than 404-ing as unknown.
    r = client.post("/control/exec",
                    json={"device_ids": [gateway.SERVER_DEVICE_ID], "command": "uptime"},
                    headers=auth)
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_connect_does_not_insert_a_phantom_device(client, auth) -> None:
    """The server is synthesized, so persisting credential edits for it would
    INSERT a row that then shows up in every cluster."""
    client.post(
        "/server/config",
        json={"ip": "10.0.0.9", "ssh_user": "dai", "ssh_password": "pw"},
        headers=auth,
    )

    client.post(
        "/control/connect",
        json={
            "device_ids": [gateway.SERVER_DEVICE_ID],
            "credentials": [{"device_id": gateway.SERVER_DEVICE_ID, "password": "other"}],
        },
        headers=auth,
    )

    listed = [d["id"] for d in client.get("/devices", headers=auth).json()]
    assert gateway.SERVER_DEVICE_ID not in listed


def test_test_endpoint_reports_each_leg(client, auth) -> None:
    client.post("/server/config", json={"ip": "10.0.0.9"}, headers=auth)

    body = client.post("/server/test", headers=auth).json()
    # No SSH user configured -> skipped, and it must not drag `ok` down on its
    # own; the card stays usable as broker-only config.
    assert body["ssh"] == "skipped"
    assert set(body) >= {"ssh", "ssh_error", "ssh_banner", "api", "broker_error"}


# ------------------------------------------------- reuse must match the target
@pytest.mark.asyncio
async def test_a_cached_connection_is_reused_for_the_same_target() -> None:
    pool = SSHPool()
    conn = AsyncMock()
    conn.is_closed = lambda: False

    with patch("asyncssh.connect", new=AsyncMock(return_value=conn)) as connect, patch(
        "app.ssh.secrets_store.get_password", return_value="pw"
    ):
        await pool.get(_device())
        await pool.get(_device())

    assert connect.await_count == 1


@pytest.mark.asyncio
async def test_editing_a_device_forces_a_reconnect() -> None:
    """Reuse keyed on the id alone silently ran commands on the previous
    machine after its host was edited -- the id still matched, so the pool
    handed back the old session."""
    pool = SSHPool()
    first, second = AsyncMock(), AsyncMock()
    first.is_closed = lambda: False
    second.is_closed = lambda: False

    moved = _device()
    moved.host = "10.0.1.99"          # same id, different machine

    with patch("asyncssh.connect", new=AsyncMock(side_effect=[first, second])) as connect, patch(
        "app.ssh.secrets_store.get_password", return_value="pw"
    ):
        await pool.get(_device())
        got = await pool.get(moved)

    assert connect.await_count == 2
    assert got is second
    assert connect.await_args.kwargs["host"] == "10.0.1.99"
    first.close.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("username", "other"), ("port", 2222)])
async def test_username_or_port_changes_also_reconnect(field, value) -> None:
    pool = SSHPool()
    first, second = AsyncMock(), AsyncMock()
    first.is_closed = lambda: False
    second.is_closed = lambda: False

    moved = _device()
    setattr(moved, field, value)

    with patch("asyncssh.connect", new=AsyncMock(side_effect=[first, second])) as connect, patch(
        "app.ssh.secrets_store.get_password", return_value="pw"
    ):
        await pool.get(_device())
        await pool.get(moved)

    assert connect.await_count == 2, f"{field} change did not reconnect"
