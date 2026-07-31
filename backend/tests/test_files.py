"""Pulling files off a device: listing, downloading, and the limits on both."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.ssh import gateway


@pytest.fixture()
def configured(client, auth, ui_export):
    client.post("/seed", json=ui_export, headers=auth)
    client.post(
        "/server/config",
        json={"ip": "10.0.0.9", "ssh_user": "dai", "ssh_password": "pw"},
        headers=auth,
    )
    return client


# ------------------------------------------------------------------ listing
def test_ls_returns_directories_first(configured, auth) -> None:
    listing = [
        {"name": "b.txt", "path": "/opt/b.txt", "dir": False, "size": 12, "mtime": 0},
        {"name": "src", "path": "/opt/src", "dir": True, "size": 0, "mtime": 0},
    ]
    with patch("app.ssh.pool.pool.get", new=AsyncMock()), patch(
        "app.ssh.commands.sftp_list", new=AsyncMock(return_value=listing)
    ):
        body = configured.get(
            f"/control/ls?device_id={gateway.SERVER_DEVICE_ID}&path=/opt", headers=auth
        ).json()

    assert body["path"] == "/opt"
    assert [e["name"] for e in body["entries"]] == ["b.txt", "src"]


def test_ls_rejects_traversal_and_credentials(configured, auth) -> None:
    for path in ("/opt/../../etc", "/home/dai/.ssh/id_rsa", "/etc/shadow"):
        r = configured.get(
            f"/control/ls?device_id={gateway.SERVER_DEVICE_ID}&path={path}", headers=auth
        )
        assert r.status_code == 400, path


# ----------------------------------------------------------------- fetching
def test_fetch_streams_the_file_back(configured, auth, tmp_path) -> None:
    async def fake_get(_conn, _remote, local: Path) -> int:
        local.write_bytes(b"cut-layer: b\n")
        return 13

    with patch("app.ssh.pool.pool.get", new=AsyncMock()), patch(
        "app.ssh.commands.sftp_get", new=AsyncMock(side_effect=fake_get)
    ):
        r = configured.get(
            f"/control/fetch?device_id={gateway.SERVER_DEVICE_ID}&path=/opt/config.yaml",
            headers=auth,
        )

    assert r.status_code == 200
    assert r.content == b"cut-layer: b\n"
    assert "config.yaml" in r.headers["content-disposition"]


def test_fetch_refuses_credentials(configured, auth) -> None:
    """Broadly allowing reads must not become a way to lift the SSH key that
    reaches every other machine in the inventory."""
    for path in ("/home/dai/.ssh/id_ed25519", "/etc/shadow", "/opt/server.pem"):
        r = configured.get(
            f"/control/fetch?device_id={gateway.SERVER_DEVICE_ID}&path={path}", headers=auth
        )
        assert r.status_code == 400, path
        assert "off-limits" in r.json()["detail"]


def test_fetch_rejects_a_directory(configured, auth) -> None:
    with patch("app.ssh.pool.pool.get", new=AsyncMock()), patch(
        "app.ssh.commands.sftp_get",
        new=AsyncMock(side_effect=IsADirectoryError("/opt is a directory")),
    ):
        r = configured.get(
            f"/control/fetch?device_id={gateway.SERVER_DEVICE_ID}&path=/opt", headers=auth
        )
    assert r.status_code == 400
    assert "directory" in r.json()["detail"]


def test_fetch_is_capped(configured, auth) -> None:
    """The file lands on this machine's disk before it is streamed on, so an
    unbounded pull is a way to fill the control plane's volume."""
    from app.routers import control

    async def huge(_conn, _remote, local: Path) -> int:
        local.write_bytes(b"x")
        return control.MAX_DOWNLOAD_BYTES + 1

    with patch("app.ssh.pool.pool.get", new=AsyncMock()), patch(
        "app.ssh.commands.sftp_get", new=AsyncMock(side_effect=huge)
    ):
        r = configured.get(
            f"/control/fetch?device_id={gateway.SERVER_DEVICE_ID}&path=/opt/big.bin",
            headers=auth,
        )
    assert r.status_code == 413


def test_fetch_needs_a_token(client) -> None:
    r = client.get(f"/control/fetch?device_id={gateway.SERVER_DEVICE_ID}&path=/opt/x")
    assert r.status_code == 401


def test_unknown_device_is_a_404(configured, auth) -> None:
    r = configured.get("/control/ls?device_id=nope&path=/opt", headers=auth)
    assert r.status_code == 404
