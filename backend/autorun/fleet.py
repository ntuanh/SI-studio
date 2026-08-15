"""Password-auth SSH driver for the lab fleet.

Consolidates the three runbooks' drivers (`split.md` §3 SSH_ASKPASS, `PA.md`
`cluster.py`, `dmsf.md` `fleet.py`) into one, because the schedule script needs
exactly one way to reach a host and three near-identical helpers is how they
drift apart.

Network shape (identical in all three guides)::

    workstation ──Tailscale──> dai ──direct-tcpip──> 192.168.101.0/24

The lab subnet is **not routed** from the workstation, so every LAN session is a
paramiko `direct-tcpip` channel opened on dai's transport. dai itself is reached
directly. Nothing is installed on any host, and no SSH key exists anywhere —
every host is password-only, which is the whole reason this file exists rather
than a two-line `ssh` call.

**No credential is hardcoded.** All five come from the environment
(`backend/.env`), so this file is safe to commit to the public repo; the
runbooks that carry the passwords inline are gitignored. Missing ones fail
loudly at startup rather than producing a confusing auth error per host.

    python fleet.py run    <alias> "<cmd>" [timeout]
    python fleet.py launch <alias> "<cmd>"          # returns immediately
    python fleet.py script <alias> <local.sh> [timeout]
    python fleet.py fanout <group> "<cmd>" [timeout]
    python fleet.py check                           # connectivity preflight

Groups: `edge` (machine-2..10), `cloud` (device-1..3), `lan`, `all`.
"""

from __future__ import annotations

import concurrent.futures as cf
import os
import subprocess
import sys
import threading

try:
    import paramiko
except ImportError:  # pragma: no cover - environment problem, not logic
    sys.stderr.write(
        "fleet.py needs paramiko. Point FLEET_PYTHON at an interpreter that has it "
        "(the guides use d:/SplitInference/venv/Scripts/python.exe).\n"
    )
    raise SystemExit(3)


def _env(name: str, default: str = "") -> str:
    v = os.environ.get(name, default)
    if not v:
        sys.stderr.write(
            f"fleet.py: {name} is not set. Fleet credentials live in backend/.env "
            "and are passed through by the schedule script.\n"
        )
        raise SystemExit(3)
    return v


JUMP_HOST = os.environ.get("FLEET_DAI_HOST", "100.68.127.89")
JUMP_USER = os.environ.get("FLEET_DAI_USER", "dai")

#: The alias that *is* this machine, if any. Set to `dai` in dai's own .env, so
#: a schedule running there executes server commands as local subprocesses
#: instead of opening an SSH session back to itself.
LOCAL_ALIAS = os.environ.get("FLEET_LOCAL_ALIAS", "").strip()

#: Connect to LAN hosts directly rather than tunnelling through dai. True on
#: dai, which sits on 192.168.101.0/24 itself; false from the workstation,
#: where that subnet is not routed. The jump would still *work* from dai --
#: it would just SSH to itself first and depend on Tailscale being up to talk
#: to the machine it is already running on.
NO_JUMP = os.environ.get("FLEET_NO_JUMP", "").strip().lower() in ("1", "true", "yes")

EDGES = [f"machine-{i}" for i in range(2, 11)]
CLOUDS = [f"device-{i}" for i in range(1, 4)]


def hosts() -> dict[str, tuple[str, int, str, str, bool]]:
    """alias -> (host, port, user, password, via_jump). Built lazily so
    `--help`-ish misuse does not demand credentials."""
    # Read leniently and validate in `connect()`: on dai itself the dai
    # password is never used (that alias runs locally), so demanding it there
    # would be asking for a credential to satisfy a code path that cannot run.
    dai_pw = os.environ.get("FLEET_DAI_PASS", "")
    edge_pw = os.environ.get("FLEET_EDGE_PASS", "")
    cloud_pw = os.environ.get("FLEET_CLOUD_PASS", "")
    via = not NO_JUMP
    h: dict[str, tuple[str, int, str, str, bool]] = {
        "dai": (JUMP_HOST, 22, JUMP_USER, dai_pw, False),
        "machine-1": ("192.168.101.91", 22, "machine-1", edge_pw, via),
    }
    for i in range(2, 11):
        h[f"machine-{i}"] = (f"192.168.101.{90 + i}", 22, f"machine-{i}", edge_pw, via)
    for i in range(1, 4):
        h[f"device-{i}"] = (f"192.168.101.{120 + i}", 22, f"device-{i}", cloud_pw, via)
    return h


def is_local(alias: str) -> bool:
    return bool(LOCAL_ALIAS) and alias == LOCAL_ALIAS


def run_local(cmd: str, timeout: int = 180) -> tuple[int, str]:
    """Run on this machine. Used when the target *is* this machine.

    `bash -lc` rather than the raw string: the runbook commands assume a login
    shell's PATH (`python3`, `pgrep`), and a schedule started by a service does
    not otherwise have one.
    """
    proc = subprocess.run(
        ["bash", "-lc", cmd],
        capture_output=True, text=True, timeout=timeout,
    )
    return proc.returncode, (proc.stdout + proc.stderr).rstrip()


def group(name: str) -> list[str]:
    return {
        "edge": EDGES,
        "cloud": CLOUDS,
        "lan": EDGES + CLOUDS,
        "all": ["dai"] + EDGES + CLOUDS,
    }.get(name, [name])


_jump = None
#: Guards creation of the shared dai connection.
_jump_lock = threading.Lock()
#: Guards *channel opening* on that connection. Deliberately a second lock:
#: taking `_jump_lock` here too would deadlock, since `jump_client()` acquires
#: it and `threading.Lock` is not reentrant.
_chan_lock = threading.Lock()


def jump_client():
    """One shared connection to dai. Every LAN channel is multiplexed onto it,
    so a 12-host fan-out costs one dai login, not twelve."""
    global _jump
    with _jump_lock:
        if _jump is None:
            c = paramiko.SSHClient()
            c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            c.connect(
                JUMP_HOST, port=22, username=JUMP_USER, password=_env("FLEET_DAI_PASS"),
                timeout=20, banner_timeout=40, auth_timeout=40,
                look_for_keys=False, allow_agent=False,
            )
            _jump = c
    return _jump


def connect(alias: str, timeout: int = 25):
    host, port, user, pw, via = hosts()[alias]
    if not pw:
        which = {"dai": "FLEET_DAI_PASS"}.get(
            alias, "FLEET_CLOUD_PASS" if alias.startswith("device-") else "FLEET_EDGE_PASS"
        )
        sys.stderr.write(
            f"fleet.py: {which} is not set, needed for {alias}. Fleet credentials "
            "live in backend/.env and are forwarded by AUTORUN_ENV_PREFIXES.\n"
        )
        raise SystemExit(3)
    sock = None
    if via:
        transport = jump_client().get_transport()
        # Serialised: opening 12 channels at once loses the SSH banner often
        # enough that PA.md documents the traceback as "harmless".
        with _chan_lock:
            sock = transport.open_channel("direct-tcpip", (host, port), ("127.0.0.1", 0))
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    last = None
    for attempt in range(3):  # banner losses under fan-out; guides retry 3x
        try:
            c.connect(
                host, port=port, username=user, password=pw, sock=sock,
                timeout=timeout, banner_timeout=40, auth_timeout=40,
                look_for_keys=False, allow_agent=False,
            )
            return c
        except paramiko.SSHException as exc:
            last = exc
            if attempt == 2 or sock is not None:
                # A consumed direct-tcpip channel cannot be reused for a retry.
                raise
    raise last  # pragma: no cover


def run(alias: str, cmd: str, timeout: int = 180) -> tuple[int, str]:
    if is_local(alias):
        return run_local(cmd, timeout)
    c = connect(alias)
    try:
        _in, out, err = c.exec_command(cmd, timeout=timeout, get_pty=False)
        text = out.read().decode("utf-8", "replace")
        errtext = err.read().decode("utf-8", "replace")
        rc = out.channel.recv_exit_status()
        return rc, (text + errtext).rstrip()
    finally:
        c.close()


def launch(alias: str, cmd: str) -> tuple[int, str]:
    """Start something that outlives the call.

    `setsid` + `< /dev/null` + a trailing `exit 0` are all load-bearing: a
    backgrounded child keeps the exec channel open even with its own streams
    redirected, so a plain `run()` blocks for the entire life of the run. This
    is the single biggest time sink the runbooks call out.
    """
    wrapped = f"({cmd}) > /dev/null 2>&1 < /dev/null & sleep 2; exit 0"
    return run(alias, wrapped, timeout=60)


def script(alias: str, path: str, timeout: int = 600) -> tuple[int, str]:
    """Pipe a real .sh file into `bash -s`.

    Never nest a loop as a heredoc inside an ssh command string — the quoting
    mangles (dmsf.md §9, split.md §3.2). A file on stdin always survives.
    """
    with open(path, "r", encoding="utf-8") as fh:
        body = fh.read()
    if is_local(alias):
        proc = subprocess.run(
            ["bash", "-s"], input=body, capture_output=True, text=True, timeout=timeout
        )
        return proc.returncode, (proc.stdout + proc.stderr).rstrip()
    c = connect(alias)
    try:
        stdin, out, err = c.exec_command("bash -s", timeout=timeout, get_pty=False)
        stdin.write(body)
        stdin.channel.shutdown_write()
        try:
            text = out.read().decode("utf-8", "replace")
            errtext = err.read().decode("utf-8", "replace")
        except EOFError:
            # A script that restarts a service on this host can tear the
            # channel down before its own `exit 0` is readable. That is the
            # deploy path working, not failing, so report what did arrive
            # rather than losing the whole transcript to an empty EOFError.
            return -1, "(channel closed early -- verify separately)"
        return out.channel.recv_exit_status(), (text + errtext).rstrip()
    finally:
        c.close()


def fanout(name: str, cmd: str, timeout: int = 180) -> int:
    """Run one command on a group. Bounded concurrency: the guides warn that
    more than ~9 simultaneous sessions truncates output."""
    targets = group(name)
    worst = 0
    with cf.ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(run, t, cmd, timeout): t for t in targets}
        for fut in cf.as_completed(futures):
            alias = futures[fut]
            try:
                rc, text = fut.result()
            except Exception as exc:  # noqa: BLE001 - report, do not abort the group
                rc, text = 1, f"ERROR {exc}"
            worst = max(worst, rc)
            print(f"===== {alias} (rc={rc}) =====")
            for line in text.splitlines():
                print(f"  {line}")
    return worst


def check() -> int:
    """Preflight: can we reach dai, one edge and one cloud?"""
    ok = True
    print(
        "mode: " + ("local dai" if is_local("dai") else "ssh dai") +
        ", LAN " + ("direct" if NO_JUMP else "via dai")
    )
    for alias in ("dai", "machine-2", "device-1"):
        try:
            rc, text = run(alias, "hostname; echo rc=$?", timeout=45)
            print(f"{alias:<10} rc={rc} {text.splitlines()[0] if text else ''}")
            ok = ok and rc == 0
        except Exception as exc:  # noqa: BLE001
            print(f"{alias:<10} UNREACHABLE: {exc}")
            ok = False
    return 0 if ok else 1


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    verb = argv[1]
    try:
        if verb == "check":
            return check()
        if verb == "fanout":
            return fanout(argv[2], argv[3], int(argv[4]) if len(argv) > 4 else 180)
        if verb == "run":
            rc, text = run(argv[2], argv[3], int(argv[4]) if len(argv) > 4 else 180)
            print(text)
            return rc
        if verb == "launch":
            rc, text = launch(argv[2], argv[3])
            print(text)
            return rc
        if verb == "script":
            rc, text = script(argv[2], argv[3], int(argv[4]) if len(argv) > 4 else 600)
            print(text)
            return rc
    except KeyError:
        print(f"unknown host {argv[2]!r}; known: dai, machine-1..10, device-1..3")
        return 2
    except Exception as exc:  # noqa: BLE001 - one clear line beats a traceback
        # The type matters as much as the message: paramiko raises a bare
        # `EOFError` when a channel closes early, and `str(EOFError())` is the
        # empty string -- which reported a restart that had actually worked as
        # "failed: " with nothing after it.
        detail = str(exc) or "no detail"
        print(f"fleet.py {verb} failed: {type(exc).__name__}: {detail}")
        return 1
    print(f"unknown verb {verb!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
