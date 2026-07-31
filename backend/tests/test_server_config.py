"""Change 1: broker/server config + connection test (backend_update.md)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.ssh import secrets_store


# --------------------------------------------------------------------- config
def test_default_config_is_empty_until_set(client, auth):
    body = client.get("/server/config", headers=auth).json()
    assert body["host"] == ""
    assert body["port"] == 5672
    assert body["api_port"] == 8000
    assert body["has_credentials"] is False
    assert "password" not in body


def test_post_config_upserts_the_singleton(client, auth):
    r = client.post(
        "/server/config",
        json={"ip": "10.0.0.5", "port": 5672, "api_port": 8000, "user": "guest", "password": "s3cret"},
        headers=auth,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["host"] == "10.0.0.5"
    assert body["ip"] == "10.0.0.5"        # UI alias
    assert body["user"] == "guest"          # UI alias
    assert body["has_credentials"] is True
    assert body["auth_ok"] is False         # spec: not tested yet

    # Second POST updates rather than creating a second row.
    r2 = client.post(
        "/server/config", json={"ip": "10.0.0.9", "port": 5673, "user": "admin"}, headers=auth
    )
    assert r2.json()["host"] == "10.0.0.9"
    assert r2.json()["port"] == 5673
    assert client.get("/server/config", headers=auth).json()["host"] == "10.0.0.9"


def test_password_is_never_returned_by_any_endpoint(client, auth):
    client.post(
        "/server/config",
        json={"ip": "10.0.0.5", "user": "guest", "password": "top-secret-pw"},
        headers=auth,
    )
    for path in ("/server/config", "/settings", "/health"):
        body = client.get(path, headers=auth).text
        assert "top-secret-pw" not in body
    assert client.get("/server/config", headers=auth).json()["has_credentials"] is True


def test_password_is_encrypted_at_rest(client, auth):
    """Acceptance: no plaintext password in the DB or the secret store file."""
    client.post(
        "/server/config",
        json={"ip": "10.0.0.5", "user": "guest", "password": "plaintext-canary"},
        headers=auth,
    )

    raw = (secrets_store._cred_path()).read_text(encoding="utf-8")
    assert "plaintext-canary" not in raw
    stored = json.loads(raw)["server-config"]
    assert stored["enc"] == "fernet"
    assert stored["password"] != "plaintext-canary"

    # ...but it decrypts back correctly for the connection test.
    assert secrets_store.get_secret("server-config", "password") == "plaintext-canary"

    # And nothing plaintext reached the SQLite file.
    from app.config import settings

    db_path = settings.database_url.split("///")[-1]
    with open(db_path, "rb") as fh:
        assert b"plaintext-canary" not in fh.read()


def test_clearing_the_password_drops_the_ref(client, auth):
    client.post(
        "/server/config", json={"ip": "10.0.0.5", "user": "guest", "password": "x"}, headers=auth
    )
    assert client.get("/server/config", headers=auth).json()["has_credentials"] is True

    client.post(
        "/server/config", json={"ip": "10.0.0.5", "user": "guest", "password": ""}, headers=auth
    )
    assert client.get("/server/config", headers=auth).json()["has_credentials"] is False


def test_config_validation(client, auth):
    assert client.post("/server/config", json={"ip": "", "user": "g"}, headers=auth).status_code == 422
    assert client.post(
        "/server/config", json={"ip": "1.2.3.4", "port": 99999, "user": "g"}, headers=auth
    ).status_code == 422


def test_config_requires_a_token(client):
    assert client.get("/server/config").status_code == 401
    assert client.post("/server/config", json={"ip": "1.2.3.4", "user": "g"}).status_code == 401


# ----------------------------------------------------------------------- test
def test_test_requires_a_configured_host(client, auth):
    r = client.post("/server/test", headers=auth)
    assert r.status_code == 400
    assert "no server host configured" in r.text


def test_test_reports_broker_version_and_api_health(client, auth):
    client.post(
        "/server/config",
        json={"ip": "10.0.0.5", "amqp_host": "10.0.0.5", "port": 5672,
              "api_port": 8000, "user": "guest", "password": "pw"},
        headers=auth,
    )

    probe = AsyncMock(return_value={"ok": True, "rabbitmq_version": "3.13.7",
                                    "product": "RabbitMQ", "error": ""})
    api = AsyncMock(return_value={"ok": True, "detail": "ok", "error": ""})
    with patch("app.routers.server.probe_broker", probe), patch("app.routers.server._probe_api", api):
        body = client.post("/server/test", headers=auth).json()

    assert body["ok"] is True
    assert body["rabbitmq_version"] == "3.13.7"
    assert body["api"] == "up"
    assert body["host"] == "10.0.0.5"

    # The probe receives credentials resolved from the secret store.
    url = probe.call_args.args[0]
    assert url.startswith("amqp://guest:pw@10.0.0.5:5672/")


def test_broker_host_is_independent_of_the_ssh_host(client, auth):
    """The machine you SSH into and the machine running RabbitMQ are often
    different; `ip` must not drag the broker along with it."""
    client.post(
        "/server/config",
        json={"ip": "100.68.127.89", "ssh_user": "dai",   # gateway
              "amqp_host": "192.168.1.20", "port": 5672, "user": "guest", "password": "pw"},
        headers=auth,
    )

    probe = AsyncMock(return_value={"ok": True, "rabbitmq_version": "4.1.0", "error": ""})
    api = AsyncMock(return_value={"ok": True, "detail": "ok", "error": ""})
    with patch("app.routers.server.probe_broker", probe), patch("app.routers.server._probe_api", api):
        client.post("/server/test", headers=auth)

    assert probe.call_args.args[0].startswith("amqp://guest:pw@192.168.1.20:5672/")

    # `$BROKER_IP` runs on the devices to measure their link to the broker, so
    # it follows the broker, not the gateway.
    assert client.get("/control/allowed-commands", headers=auth).json()["broker_ip"] == "192.168.1.20"


def test_broker_host_falls_back_to_the_configured_broker_url(client, auth):
    """Blank `amqp_host` means "the broker this backend already uses" -- a far
    better default than the SSH gateway, which often runs no broker."""
    from app.routers.server import _settings_broker_host

    out = client.post("/server/config", json={"ip": "100.68.127.89"}, headers=auth).json()

    assert out["amqp_host"] == ""
    assert out["amqp_host_resolved"] == _settings_broker_host()
    assert out["amqp_host_resolved"] != "100.68.127.89"


def test_test_reports_failure_without_raising(client, auth):
    client.post("/server/config", json={"ip": "10.0.0.5", "user": "guest"}, headers=auth)

    probe = AsyncMock(return_value={"ok": False, "rabbitmq_version": "", "product": "",
                                    "error": "ConnectionRefusedError"})
    api = AsyncMock(return_value={"ok": False, "detail": "", "error": "URLError"})
    with patch("app.routers.server.probe_broker", probe), patch("app.routers.server._probe_api", api):
        body = client.post("/server/test", headers=auth).json()

    assert body["ok"] is False
    assert body["api"] == "down"
    assert "ConnectionRefused" in body["broker_error"]


def test_partial_failure_is_not_ok(client, auth):
    """Broker up but API down must not report ok."""
    client.post("/server/config", json={"ip": "10.0.0.5", "user": "guest"}, headers=auth)

    probe = AsyncMock(return_value={"ok": True, "rabbitmq_version": "3.13.7", "product": "", "error": ""})
    api = AsyncMock(return_value={"ok": False, "detail": "", "error": "refused"})
    with patch("app.routers.server.probe_broker", probe), patch("app.routers.server._probe_api", api):
        body = client.post("/server/test", headers=auth).json()

    assert body["ok"] is False
    assert body["rabbitmq_version"] == "3.13.7"
    assert body["api"] == "down"


def test_test_broadcasts_server_status_over_the_websocket(client, auth):
    client.post("/server/config", json={"ip": "10.0.0.5", "user": "guest"}, headers=auth)

    with client.websocket_connect("/ws/stream?token=test-token") as ws:
        ws.receive_json()  # snapshot

        probe = AsyncMock(return_value={"ok": True, "rabbitmq_version": "3.13.7",
                                        "product": "RabbitMQ", "error": ""})
        api = AsyncMock(return_value={"ok": True, "detail": "ok", "error": ""})
        with patch("app.routers.server.probe_broker", probe), patch("app.routers.server._probe_api", api):
            client.post("/server/test", headers=auth)

        frame = ws.receive_json()
        assert frame["type"] == "server_status"
        assert frame["status"] == "on"
        assert frame["rabbitmq_version"] == "3.13.7"
        assert frame["api"] == "up"


def test_failed_test_broadcasts_error_status(client, auth):
    client.post("/server/config", json={"ip": "10.0.0.5", "user": "guest"}, headers=auth)

    with client.websocket_connect("/ws/stream?token=test-token") as ws:
        ws.receive_json()
        probe = AsyncMock(return_value={"ok": False, "rabbitmq_version": "", "product": "", "error": "boom"})
        api = AsyncMock(return_value={"ok": False, "detail": "", "error": "boom"})
        with patch("app.routers.server.probe_broker", probe), patch("app.routers.server._probe_api", api):
            client.post("/server/test", headers=auth)

        frame = ws.receive_json()
        assert frame["type"] == "server_status"
        assert frame["status"] == "error"


def test_server_status_is_replayed_in_the_snapshot(client, auth):
    client.post("/server/config", json={"ip": "10.0.0.5", "user": "guest"}, headers=auth)
    probe = AsyncMock(return_value={"ok": True, "rabbitmq_version": "3.13.7", "product": "", "error": ""})
    api = AsyncMock(return_value={"ok": True, "detail": "ok", "error": ""})
    with patch("app.routers.server.probe_broker", probe), patch("app.routers.server._probe_api", api):
        client.post("/server/test", headers=auth)

    with client.websocket_connect("/ws/stream?token=test-token") as ws:
        snapshot = ws.receive_json()
        assert snapshot["server_status"] == "on"


def test_changing_config_resets_the_tested_status(client, auth):
    from app.services.server_state import server_state

    client.post("/server/config", json={"ip": "10.0.0.5", "user": "guest"}, headers=auth)
    probe = AsyncMock(return_value={"ok": True, "rabbitmq_version": "3.13.7", "product": "", "error": ""})
    api = AsyncMock(return_value={"ok": True, "detail": "ok", "error": ""})
    with patch("app.routers.server.probe_broker", probe), patch("app.routers.server._probe_api", api):
        client.post("/server/test", headers=auth)
    assert server_state.status == "on"

    client.post("/server/config", json={"ip": "10.0.0.99", "user": "guest"}, headers=auth)
    assert server_state.status == "off"


# ------------------------------------------------------------- probe_broker
def test_probe_broker_never_raises_on_a_dead_host():
    import asyncio

    from app.inference.broker import probe_broker

    result = asyncio.run(probe_broker("amqp://guest:guest@127.0.0.1:1/", timeout=2))
    assert result["ok"] is False
    assert result["error"]
    assert result["rabbitmq_version"] == ""


@pytest.mark.parametrize("bad_url", ["not-a-url", "amqp://", "http://wrong-scheme:5672/"])
def test_probe_broker_handles_malformed_urls(bad_url):
    import asyncio

    from app.inference.broker import probe_broker

    result = asyncio.run(probe_broker(bad_url, timeout=2))
    assert result["ok"] is False
