"""Operator-configurable command presets, and the working directory.

`cd` is the motivating case for both: it is not on the allow-list, and even if
it were it could not work, because every command runs in its own shell.
"""

from __future__ import annotations

from app.ssh import commands as cmds


# ------------------------------------------------------------ working directory
def test_working_directory_travels_with_every_command() -> None:
    assert cmds.with_working_directory("ls", "/opt/app") == "cd /opt/app && ls"
    assert cmds.with_working_directory("ls", "") == "ls"
    assert cmds.with_working_directory("ls", None) == "ls"


def test_working_directory_is_quoted() -> None:
    """The path is operator input; `&&` around it is ours. A path carrying
    shell syntax must not become a second command."""
    built = cmds.with_working_directory("ls", "/tmp/a; rm -rf /")
    assert built == "cd '/tmp/a; rm -rf /' && ls"

    built = cmds.with_working_directory("ls", "/tmp/$(whoami)")
    assert "$(whoami)" in built and built.startswith("cd '")


def test_working_directory_rejects_newlines() -> None:
    import pytest

    with pytest.raises(cmds.CommandRejected):
        cmds.with_working_directory("ls", "/tmp\nrm -rf /")


def test_cwd_is_applied_after_validation(client, auth) -> None:
    """The guard must see what the operator typed. If it saw the wrapper, the
    `&&` we add would trip the metacharacter check on every single command."""
    client.post(
        "/server/config",
        json={"ip": "10.0.0.9", "ssh_user": "dai", "ssh_password": "pw"},
        headers=auth,
    )

    r = client.post(
        "/control/exec",
        json={"device_ids": ["__server__"], "command": "ls", "cwd": "/opt/app"},
        headers=auth,
    )
    assert r.status_code == 200
    assert r.json()["command"] == "cd /opt/app && ls"


def test_cd_is_still_refused_with_a_pointer_to_the_fix(client, auth, ui_export) -> None:
    client.post("/seed", json=ui_export, headers=auth)
    r = client.post(
        "/control/exec",
        json={"device_ids": ["dA"], "command": "cd /opt/app"},
        headers=auth,
    )
    assert r.status_code == 400
    assert "not on the allow-list" in r.json()["detail"]
    assert "presets" in r.json()["detail"]  # tells them where to fix it


# -------------------------------------------------------------------- presets
def test_presets_default_until_customised(client, auth) -> None:
    body = client.get("/control/presets", headers=auth).json()
    assert body["custom"] is False
    labels = [p["label"] for p in body["presets"]]
    assert "nvidia-smi" in labels and "reboot" in labels


def test_presets_round_trip_and_keep_order(client, auth) -> None:
    saved = client.put(
        "/control/presets",
        json={"presets": [
            {"label": "list", "command": "ls -la"},
            {"label": "log", "command": "git log --oneline -10"},
        ]},
        headers=auth,
    ).json()

    assert saved["custom"] is True
    assert [p["label"] for p in saved["presets"]] == ["list", "log"]
    assert client.get("/control/presets", headers=auth).json() == saved


#: Not on the built-in allow-list, and unlikely ever to be -- running a project's
#: own tooling is exactly the case presets exist for.
CUSTOM_COMMAND = "pytest -q tests/"


def test_a_saved_preset_becomes_runnable(client, auth, ui_export) -> None:
    """The whole point: writing a command down is the operator saying they
    mean it, which is the thing the allow-list cannot establish on its own."""
    client.post("/seed", json=ui_export, headers=auth)

    before = client.post(
        "/control/exec",
        json={"device_ids": ["dA"], "command": CUSTOM_COMMAND},
        headers=auth,
    )
    assert before.status_code == 400

    client.put(
        "/control/presets",
        json={"presets": [{"label": "tests", "command": CUSTOM_COMMAND}]},
        headers=auth,
    )

    after = client.post(
        "/control/exec",
        json={"device_ids": ["dA"], "command": CUSTOM_COMMAND},
        headers=auth,
    )
    # Past the guard now -- it fails on connection instead (no host in tests).
    assert after.status_code == 200
    assert after.json()["ok"] is False


def test_a_saved_preset_cannot_unlock_credentials(client, auth, ui_export) -> None:
    """Presets are an operator's own shortcuts, not a way around the one thing
    the control API must not do with someone else's SSH login."""
    client.post("/seed", json=ui_export, headers=auth)
    client.put(
        "/control/presets",
        json={"presets": [{"label": "keys", "command": "cat /etc/shadow"}]},
        headers=auth,
    )

    r = client.post(
        "/control/exec",
        json={"device_ids": ["dA"], "command": "cat /etc/shadow"},
        headers=auth,
    )
    assert r.status_code == 400
    assert "off-limits" in r.json()["detail"]


def test_interactive_programs_fail_fast(client, auth, ui_export) -> None:
    """`vim` over an exec channel has no terminal to draw on and no keyboard to
    read; without this it burns the whole command timeout in silence."""
    client.post("/seed", json=ui_export, headers=auth)

    for command in ("vim config.yaml", "sudo nano /etc/hosts", "less app.log", "top"):
        r = client.post(
            "/control/exec",
            json={"device_ids": ["dA"], "command": command},
            headers=auth,
        )
        assert r.status_code == 400, command
        assert "interactive terminal" in r.json()["detail"]

    # A batch python run is not interactive and must still pass.
    r = client.post(
        "/control/exec",
        json={"device_ids": ["dA"],
              "command": 'python -c "import platform;print(platform.python_version())"'},
        headers=auth,
    )
    assert r.status_code == 200


def test_saving_a_preset_does_not_open_the_gate_generally(client, auth, ui_export) -> None:
    client.post("/seed", json=ui_export, headers=auth)
    client.put(
        "/control/presets",
        json={"presets": [{"label": "log", "command": "git log --oneline -10"}]},
        headers=auth,
    )

    # A *different* command is still judged on its own merits, and a preset is
    # matched exactly rather than by prefix -- otherwise saving `git log` would
    # admit `git log; rm -rf /`.
    for command in ("curl http://evil.example", "git log --oneline -10; rm -rf /"):
        r = client.post(
            "/control/exec",
            json={"device_ids": ["dA"], "command": command},
            headers=auth,
        )
        assert r.status_code == 400, command


def test_presets_reset(client, auth) -> None:
    client.put(
        "/control/presets",
        json={"presets": [{"label": "x", "command": "ls"}]},
        headers=auth,
    )
    body = client.post("/control/presets/reset", headers=auth).json()
    assert body["custom"] is False
    assert client.get("/control/presets", headers=auth).json()["custom"] is False


# ---------------------------------------------------------------- directories
def test_directories_round_trip_with_derived_labels(client, auth) -> None:
    body = client.put(
        "/control/directories",
        json={"directories": [
            {"path": "ntuanh/Optimizer/split_inference_test"},
            {"label": "logs", "path": "/var/log"},
            {"path": "/opt/app/"},
            {"label": "ignored", "path": "  "},
        ]},
        headers=auth,
    ).json()

    # The label defaults to the last segment: that is what distinguishes one
    # saved directory from another, and the full path lives on the tooltip.
    assert [(d["label"], d["path"]) for d in body["directories"]] == [
        ("split_inference_test", "ntuanh/Optimizer/split_inference_test"),
        ("logs", "/var/log"),
        ("app", "/opt/app/"),
    ]
    assert client.get("/control/directories", headers=auth).json() == body


def test_directories_replace_rather_than_append(client, auth) -> None:
    client.put(
        "/control/directories",
        json={"directories": [{"path": "/one"}, {"path": "/two"}]},
        headers=auth,
    )
    body = client.put(
        "/control/directories", json={"directories": [{"path": "/three"}]}, headers=auth
    ).json()

    assert [d["path"] for d in body["directories"]] == ["/three"]


def test_a_saved_directory_grants_nothing(client, auth, ui_export) -> None:
    """A path only ever reaches `cd`; whatever runs inside it is judged exactly
    as it would be anywhere else."""
    client.post("/seed", json=ui_export, headers=auth)
    client.put(
        "/control/directories", json={"directories": [{"path": "/opt/app"}]}, headers=auth
    )

    r = client.post(
        "/control/exec",
        json={"device_ids": ["dA"], "command": "curl http://evil.example", "cwd": "/opt/app"},
        headers=auth,
    )
    assert r.status_code == 400
    assert "allow-list" in r.json()["detail"]


def test_blank_commands_are_dropped_and_labels_default(client, auth) -> None:
    body = client.put(
        "/control/presets",
        json={"presets": [
            {"label": "", "command": "uptime"},
            {"label": "ignored", "command": "   "},
        ]},
        headers=auth,
    ).json()

    assert len(body["presets"]) == 1
    assert body["presets"][0]["label"] == "uptime"
