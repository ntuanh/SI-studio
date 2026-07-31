#!/usr/bin/env python3
"""Smoke test for the Split Inference Control Plane.

Exercises the endpoints end to end so you can confirm the backend works. Uses
only the standard library, and runs regardless of the PowerShell execution
policy -- unlike smoke.ps1, which needs `-ExecutionPolicy Bypass` on machines
where LocalMachine is AllSigned.

    .venv\\Scripts\\python.exe smoke.py
    .venv\\Scripts\\python.exe smoke.py --port 8001
    .venv\\Scripts\\python.exe smoke.py --base-url http://192.168.1.20:8000 --token my-token

Safe to re-run: /seed replaces the inventory each time.
Exit code is 0 when every check passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent

# --- colours: on for a real terminal, off when piped or unsupported ---
if os.name == "nt":
    os.system("")  # enables ANSI on Windows 10+ consoles
_TTY = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text


CYAN = lambda s: _c("36", s)      # noqa: E731
GREEN = lambda s: _c("32", s)     # noqa: E731
RED = lambda s: _c("31;1", s)     # noqa: E731
GREY = lambda s: _c("90", s)      # noqa: E731
YELLOW = lambda s: _c("33", s)    # noqa: E731

PASS = 0
FAIL = 0


def step(n: int, text: str) -> None:
    print(f"\n{CYAN(f'[{n}]')} {text}")


def ok(text: str) -> None:
    global PASS
    PASS += 1
    print(f"    {GREEN('OK')}   {text}")


def bad(text: str) -> None:
    global FAIL
    FAIL += 1
    print(f"    {RED('FAIL')} {text}")


def info(text: str) -> None:
    print(GREY(f"         {text}"))


class ApiError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


def api(
    method: str, path: str, body: Any = None, *, base: str, token: str, timeout: float = 30
) -> Any:
    """One request. Raises ApiError for any non-2xx."""
    data = None
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(f"{base}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - local http
            raw = resp.read().decode("utf-8", "replace")
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        detail = raw
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                detail = str(parsed.get("detail", raw))
        except json.JSONDecodeError:
            pass
        raise ApiError(exc.code, detail) from exc


def read_token_from_env_file() -> str:
    env_file = HERE / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if m := re.match(r"^API_TOKEN\s*=\s*(.*)$", line.strip()):
                return m.group(1).strip()
    return "dev-token-change-me"


SCENARIO = {
    "model": "yolov11n",
    "config": {
        "clustering": True, "numClusters": 2, "autoBalance": "power",
        "manualEnabled": False, "manualSplit": 5, "modelName": "yolov11n",
    },
    "stages": [
        {"id": "s1", "kind": "Edge", "name": "Edge", "devices": [
            {"id": "dA", "name": "Jetson-A", "gflops": 472, "bw": 12, "lat": 6, "cluster": 1},
            {"id": "dB", "name": "Jetson-B", "gflops": 472, "bw": 12, "lat": 6, "cluster": 1},
            {"id": "dC", "name": "Jetson-C", "gflops": 384, "bw": 10, "lat": 8, "cluster": 2}]},
        {"id": "s2", "kind": "Cloud", "name": "Cloud", "devices": [
            {"id": "dG1", "name": "GPU-1", "gflops": 9800, "bw": 125, "lat": 2, "cluster": 1},
            {"id": "dG2", "name": "GPU-2", "gflops": 9800, "bw": 125, "lat": 2, "cluster": 2}]},
    ],
    "clusters": [],
}


def main() -> int:
    ap = argparse.ArgumentParser(description="Smoke test the control plane")
    ap.add_argument("--base-url", default="")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--token", default="")
    args = ap.parse_args()

    base = args.base_url.rstrip("/") or f"http://127.0.0.1:{args.port}"
    token = args.token or read_token_from_env_file()
    call = lambda m, p, b=None: api(m, p, b, base=base, token=token)  # noqa: E731

    print("Split Inference Control Plane -- smoke test")
    print(GREY(f"target: {base}"))

    # ----------------------------------------------------------- 1. reachable
    step(1, "Server reachable and healthy")
    try:
        health = api("GET", "/health", base=base, token="", timeout=10)
    except (ApiError, OSError) as exc:
        bad(f"cannot reach {base}/health ({exc})")
        print(YELLOW("\n  Is the server running? Start it with:"))
        print(YELLOW("    .venv\\Scripts\\python.exe -m uvicorn app.main:app --reload\n"))
        return 1
    ok(f"status={health['status']}")
    if health.get("broker_connected"):
        ok("RabbitMQ connected")
    else:
        info("RabbitMQ NOT connected -- Simulate mode and the Control tab still work,")
        info("but /run/start returns 503. Start one with:")
        info("  docker run -d -p 5672:5672 -p 15672:15672 rabbitmq:3.13-management-alpine")

    # ---------------------------------------------------------------- 2. auth
    step(2, "Auth is enforced")
    try:
        api("GET", "/devices", base=base, token="", timeout=10)
        bad("/devices answered without a token")
    except ApiError as exc:
        if exc.status == 401:
            ok("unauthenticated request rejected (401)")
        else:
            bad(f"expected 401, got {exc.status}")
    try:
        call("GET", "/devices")
        ok("token accepted")
    except ApiError:
        bad("token rejected -- check API_TOKEN in .env matches --token")
        return 1

    # ---------------------------------------------------------------- 3. seed
    step(3, "Load the UI's default scenario (POST /seed)")
    seeded = call("POST", "/seed", SCENARIO)
    if seeded["devices"] == 5:
        ok("5 devices imported across 2 clusters")
    else:
        bad(f"expected 5 devices, got {seeded['devices']}")

    # ------------------------------------------------------------- 4. metrics
    step(4, "Metrics match the UI simulator (GET /metrics/latest)")
    m = call("GET", "/metrics/latest")
    for c in m["clusters"]:
        info(
            f"cluster {c['cluster']}: cut@{c['cut']}/{c.get('layer_count')}  "
            f"e2e {c['e2e_ms']}ms  {c['fps']} fps  msg {c['msg_mb']}MB  [{c['source']}]"
        )
    # Golden values cross-checked against the UI's JS in an independent engine.
    c1 = next(c for c in m["clusters"] if c["cluster"] == 1)
    if c1["cut"] == 2 and abs(c1["fps"] - 33.621) < 0.01:
        ok("cluster 1 reproduces the UI exactly (cut@2, 33.621 fps)")
    else:
        bad(f"cluster 1 drifted from the UI: cut={c1['cut']} fps={c1['fps']}")
    if abs(m["aggregate_fps"] - 49.703) < 0.01:
        ok("aggregate 49.703 fps")
    else:
        bad(f"aggregate drifted: {m['aggregate_fps']}")

    # ------------------------------------------------------ 5. cut selection
    step(5, "Switching to latency mode changes the cut")
    call("PATCH", "/config", {"autoBalance": "latency"})
    cuts = [c["cut"] for c in call("GET", "/metrics/latest")["clusters"]]
    if cuts == [8, 8]:
        ok("latency mode picks cut 8 for both clusters")
    else:
        bad(f"expected cuts [8, 8] -- got {cuts}")
    call("PATCH", "/config", {"autoBalance": "power"})
    info("restored power balancing")

    # -------------------------------------------------------- 6. command guards
    step(6, "Command allow-list and confirmation gate")
    try:
        call("POST", "/control/exec", {"device_ids": ["dA"], "command": "uptime"})
        ok("'uptime' accepted by the guard")
        info("SSH itself failed as expected (dA has no host configured)")
    except ApiError as exc:
        bad(f"'uptime' was rejected: {exc}")

    for danger in ("rm -rf /", "nvidia-smi; rm -rf /", "cat /etc/shadow"):
        try:
            call("POST", "/control/exec", {"device_ids": ["dA"], "command": danger})
            bad(f"{danger!r} was NOT blocked")
        except ApiError as exc:
            if exc.status == 400:
                ok(f"blocked: {danger!r}")
            else:
                bad(f"{danger!r} gave {exc.status}, expected 400")

    try:
        call("POST", "/control/exec", {"device_ids": ["dA"], "command": "sudo reboot"})
        bad("'sudo reboot' ran without confirmation")
    except ApiError as exc:
        if exc.status == 409:
            ok("'sudo reboot' needs confirm=true (409)")
        else:
            bad(f"expected 409, got {exc.status}")
    try:
        call("POST", "/control/exec",
             {"device_ids": ["dA"], "command": "sudo reboot", "confirm": True})
        ok("'sudo reboot' allowed with confirm=true")
    except ApiError:
        bad("confirmed reboot was still refused")

    py_preset = 'python -c "import torch;print(torch.__version__, torch.cuda.is_available())"'
    try:
        call("POST", "/control/exec", {"device_ids": ["dA"], "command": py_preset})
        ok("python version preset accepted (quoted ';' handled)")
    except ApiError as exc:
        bad(f"python preset rejected: {exc}")

    # ------------------------------------------------------ 7. broker/server
    step(7, "Broker config + connection test")
    api_port = int(base.rsplit(":", 1)[-1]) if base.rsplit(":", 1)[-1].isdigit() else 8000
    call("POST", "/server/config", {
        "ip": "127.0.0.1", "port": 5672, "api_port": api_port,
        "user": "guest", "password": "guest",
    })
    cfg = call("GET", "/server/config")
    if cfg.get("has_credentials") and "password" not in cfg:
        ok(f"password stored but never returned (has_credentials={cfg['has_credentials']})")
    else:
        bad("password handling looks wrong")

    t = call("POST", "/server/test")
    if t["ok"]:
        ok(f"broker reachable: RabbitMQ {t['rabbitmq_version']} / API {t['api']}")
    else:
        info(f"test reported not-ok -- broker: {t['broker_error']!r} api: {t['api']!r}")
        info("(expected if RabbitMQ isn't running)")

    # ---------------------------------------------------------- 8. BROKER_IP
    step(8, "$BROKER_IP substitution")
    try:
        r = call("POST", "/control/exec",
                 {"device_ids": ["dA"], "command": "ping -c 3 $BROKER_IP"})
        if r["command"] == "ping -c 3 127.0.0.1":
            ok(f"substituted -> {r['command']!r}")
        else:
            bad(f"unexpected substitution: {r['command']!r}")
    except ApiError as exc:
        bad(f"BROKER_IP command failed: {exc}")

    # -------------------------------------------------------------- 9. audit
    step(9, "Audit trail for destructive commands")
    entries = call("GET", "/control/audit")["entries"]
    if len(entries) >= 2:
        ok(f"{len(entries)} audit entries recorded")
        for e in entries[:3]:
            info(f"{e['action']}: {e['command']!r} confirmed={e['confirmed']} -> {e['outcome']}")
    else:
        bad(f"expected at least 2 audit entries, found {len(entries)}")

    # ------------------------------------------------------------- summary
    print("\n" + "-" * 60)
    if FAIL == 0:
        print(GREEN(f"All {PASS} checks passed."))
    else:
        print(RED(f"{PASS} passed, {FAIL} FAILED."))
    print(GREY(f"""
Next:
  Swagger UI    {base}/docs   (Authorize with: {token})
  Metrics       {base}/metrics/latest
  Wire the UI   see ui{os.sep}WIRING.md
"""))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
