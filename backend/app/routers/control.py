"""SSH / SCP / fan-out endpoints backing the Control tab (guide §7)."""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

import asyncssh
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from sqlmodel import desc, select
from sqlmodel.ext.asyncio.session import AsyncSession

from ..auth import require_token
from ..config import settings
from ..db import get_session
from ..models import AuditLog, CommandPreset, Device, DirectoryPreset, ServerConfig
from ..schemas import (
    ConnectRequest,
    DisconnectRequest,
    ExecRequest,
    DirectoriesIn,
    ExecResponse,
    ExecStopRequest,
    PresetsIn,
    StatusOut,
)
from ..services.metrics_bus import bus
from ..services.server_state import server_state
from ..ssh import commands as cmds
from ..ssh import gateway, secrets_store
from ..ssh.commands import (
    ALLOWED_MODULE_PREFIXES,
    ALLOWED_PREFIXES,
    CommandRejected,
    ConfirmationRequired,
)
from ..ssh.pool import SSHError, pool

log = logging.getLogger(__name__)

router = APIRouter(prefix="/control", tags=["control"], dependencies=[Depends(require_token)])

#: Guard rail for uploads pushed through /control/scp.
MAX_UPLOAD_BYTES = 2 * 1024**3  # 2 GiB


async def _resolve_devices(session: AsyncSession, device_ids: list[str]) -> list[Device]:
    """Look up targets by id, resolving the reserved server id specially.

    `__server__` is the control server (the Control tab's top card). It is a
    real SSH target but deliberately not a `device` row -- see
    `ssh/gateway.py` -- so it is synthesized here instead of being selected.
    """
    if not device_ids:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "device_ids must not be empty")

    wanted = [i for i in device_ids if not gateway.is_server(i)]
    rows = (await session.exec(select(Device).where(Device.id.in_(wanted)))).all() if wanted else []
    found = {d.id: d for d in rows}

    if any(gateway.is_server(i) for i in device_ids):
        cfg = await session.get(ServerConfig, 1)
        if cfg is None or not gateway.is_configured(cfg):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "the control server has no SSH host/username configured; "
                "fill in the Broker / backend server card first",
            )
        found[gateway.SERVER_DEVICE_ID] = gateway.server_device(cfg)

    if missing := [i for i in device_ids if i not in found]:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown device ids: {missing}")
    return [found[i] for i in device_ids]


# ------------------------------------------------------------------- connect
@router.post("/connect")
async def connect(
    payload: ConnectRequest, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """Open sessions. Credentials from the UI's per-device form are applied to
    the device record first; passwords go to the secret store, never the DB."""
    creds = {c.device_id: c for c in payload.credentials}
    ids = payload.device_ids or list(creds)
    devices = await _resolve_devices(session, ids)

    one_shot: dict[str, str] = {}
    dirty = False
    for d in devices:
        c = creds.get(d.id)
        if c is None:
            continue
        if gateway.is_server(d.id):
            # Synthesized from ServerConfig, not a row -- adding it to the
            # session would INSERT a phantom device that then shows up in
            # every cluster. Its settings are edited via POST /server/config;
            # accept a one-shot password here and nothing else.
            if c.password:
                one_shot[d.id] = c.password
            continue
        if c.host is not None:
            d.host, dirty = c.host, True
        if c.port is not None:
            d.port, dirty = c.port, True
        if c.username is not None:
            d.username, dirty = c.username, True
        if c.key_ref is not None:
            d.key_ref, dirty = c.key_ref, True
        if c.auth_method is not None:
            d.auth_method, dirty = c.auth_method, True
        elif c.password:
            d.auth_method, dirty = "password", True
        if c.password:
            if c.remember:
                secrets_store.set_password(d.id, c.password)
            else:
                one_shot[d.id] = c.password
        if dirty:
            session.add(d)

    if dirty:
        await session.commit()
        for d in devices:
            await session.refresh(d)

    bus.exec_line("", "⇄ opening SSH sessions…", "meta")
    results = await pool.connect_all(devices, passwords=one_shot)

    for d in devices:
        r = results.get(d.id, {})
        if r.get("status") == "on":
            bus.exec_line(d.id, f"✓ {d.username}@{d.host} connected", "stdout")
        else:
            bus.exec_line(d.id, f"✗ {d.username}@{d.host}: {r.get('detail', 'failed')}", "stderr")

    connected = sum(1 for r in results.values() if r.get("status") == "on")
    return {
        "requested": len(devices),
        "connected": connected,
        "failed": len(devices) - connected,
        "results": results,
        "statuses": pool.statuses(),
    }


@router.post("/disconnect")
async def disconnect(payload: DisconnectRequest) -> dict[str, Any]:
    closed = await pool.disconnect_all(payload.device_ids)
    bus.exec_line("", "⇄ all sessions closed", "meta")
    return {"closed": closed, "statuses": pool.statuses()}


@router.get("/status", response_model=StatusOut)
async def statuses() -> StatusOut:
    return StatusOut(statuses=pool.statuses())


# ---------------------------------------------------------------------- exec
@router.post("/exec", response_model=ExecResponse)
async def exec_command(
    payload: ExecRequest, session: AsyncSession = Depends(get_session)
) -> ExecResponse:
    """Fan a single command across the selected devices.

    Operator input, so the allow-list guard applies here (see
    `commands.validate_command`). `$BROKER_IP` is substituted *before*
    validation, so the allow-list sees the string that will reach the shell.
    Destructive presets need `confirm=true` and are written to the audit log.

    Commands run under a pty and stream their output live, so this answers
    without waiting for a long one to finish: anything still going after
    `detach_after` seconds is reported in `running` and keeps streaming to the
    console. That is what makes `python3 Server.py` usable from here -- it runs
    for as long as the experiment does, which no HTTP request can be held open
    for. Stop it with `POST /control/exec/stop`.
    """
    saved = {
        p.command for p in (await session.exec(select(CommandPreset))).all()
    }
    try:
        command = cmds.substitute_broker_ip(payload.command, server_state.host)
        # Checked before the allow-list so saving `vim` as a preset -- which
        # otherwise makes it runnable -- still explains why it cannot work,
        # rather than hanging until the command timeout.
        cmds.reject_if_interactive(command)
        command = cmds.validate_command(command, confirm=payload.confirm, saved=saved)
        # After validation: the guard must inspect what the operator typed, not
        # the `cd … && …` wrapper this service builds around it.
        command = cmds.with_working_directory(command, payload.cwd)
    except ConfirmationRequired as exc:
        await _audit(
            session, "exec_denied", payload.command, payload.device_ids,
            confirmed=False, outcome="confirmation required",
        )
        # 409: the request is well-formed but needs an explicit acknowledgement.
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except CommandRejected as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    devices = await _resolve_devices(session, payload.device_ids)
    destructive = cmds.is_destructive(command)
    if destructive:
        log.warning(
            "destructive command confirmed: %r on %s", command, payload.device_ids
        )
        bus.exec_line("", f"⚠ destructive: {command}", "meta")

    bus.exec_line("", f"$ {command}   → {len(devices)} host(s)", "meta")

    settle = (
        settings.exec_detach_after if payload.detach_after is None else payload.detach_after
    )
    results, running = await cmds.fan_out_jobs(
        pool, devices, command, settle=settle, timeout=payload.timeout
    )

    for job in running:
        # Said in the console rather than only in the response body, because
        # the console is where the operator is looking and the UI does not
        # render the response.
        bus.exec_line(
            job.device_id,
            f"│  … still running on {job.device_name} — type ^C and Run to stop it",
            "meta",
        )

    # A command that is still going has not failed; only the ones that actually
    # finished can say anything about success.
    ok = all(r.ok for r in results)

    if destructive:
        await _audit(
            session, "exec", command, payload.device_ids,
            confirmed=True,
            outcome="ok" if ok else "; ".join(
                f"{r.device_id}:{r.error or r.exit}" for r in results if not r.ok
            )[:400],
        )

    return ExecResponse(
        command=command,
        results=[r.to_dict() for r in results],
        ok=ok,
        running=[j.to_dict() for j in running],
    )


@router.post("/exec/stop")
async def stop_exec(payload: ExecStopRequest) -> dict[str, Any]:
    """Ctrl-C: stop commands left running by `/control/exec`.

    Escalates SIGINT -> SIGTERM -> channel close (see `JobRegistry.interrupt`),
    so a run that writes its results on the way out gets the chance to.
    """
    if payload.job_id:
        job = cmds.jobs.get(payload.job_id)
        if job is None or not job.running:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, f"no running job {payload.job_id!r}"
            )
        targets = [job]
    else:
        targets = cmds.jobs.running(payload.device_ids or None)

    if not targets:
        bus.exec_line("", "^C  nothing was running", "meta")
        return {"stopped": [], "note": "nothing was running"}

    for job in targets:
        bus.exec_line(job.device_id, f"^C  {job.command}", "meta")

    # Concurrently: each interrupt walks its own escalation ladder, and doing
    # twelve machines one after another would take minutes.
    outcomes = await asyncio.gather(*(cmds.jobs.interrupt(j) for j in targets))

    stopped = []
    for job, outcome in zip(targets, outcomes):
        bus.exec_line(job.device_id, f"└─ {outcome}", "meta")
        stopped.append({**job.to_dict(), "outcome": outcome})
    return {"stopped": stopped}


@router.get("/jobs")
async def list_jobs() -> dict[str, Any]:
    """Commands still running, across every device."""
    return {"jobs": [j.to_dict() for j in cmds.jobs.running()]}


async def _audit(
    session: AsyncSession,
    action: str,
    command: str,
    device_ids: list[str],
    *,
    confirmed: bool,
    outcome: str,
) -> None:
    session.add(
        AuditLog(
            id=uuid.uuid4().hex[:12],
            action=action,
            command=command[:500],
            device_ids=",".join(device_ids)[:500],
            confirmed=confirmed,
            outcome=outcome,
        )
    )
    await session.commit()


@router.get("/audit")
async def audit_log(
    limit: int = 50, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """Destructive commands run (or refused) through /control/exec."""
    rows = (
        await session.exec(
            select(AuditLog).order_by(desc(AuditLog.at)).limit(max(1, min(500, limit)))
        )
    ).all()
    return {
        "entries": [
            {
                "id": r.id, "action": r.action, "command": r.command,
                "device_ids": [d for d in r.device_ids.split(",") if d],
                "confirmed": r.confirmed, "outcome": r.outcome, "at": r.at,
            }
            for r in rows
        ]
    }


# ------------------------------------------------------------------- presets
#: What the UI ships with. Kept here rather than in the page so the two cannot
#: drift, and so a fresh database starts with something usable.
DEFAULT_PRESETS: list[dict[str, str]] = [
    {"label": "nvidia-smi", "command": "nvidia-smi"},
    {"label": "uptime", "command": "uptime"},
    {"label": "nproc", "command": "nproc"},
    {"label": "disk", "command": "df -h"},
    {"label": "memory", "command": "free -h"},
    {"label": "GPU temp",
     "command": "nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader"},
    {"label": "iperf3", "command": "iperf3 -c $BROKER_IP -t 5"},
    {"label": "ping broker", "command": "ping -c 3 $BROKER_IP"},
    {"label": "agent status", "command": "systemctl status inference-agent"},
    {"label": "restart agent", "command": "systemctl restart inference-agent"},
    {"label": "stop agent", "command": "systemctl stop inference-agent"},
    {"label": "tail logs", "command": "journalctl -u inference-agent -n 50 --no-pager"},
    {"label": "python ver",
     "command": 'python -c "import torch;print(torch.__version__, torch.cuda.is_available())"'},
    {"label": "reboot", "command": "sudo reboot"},
]


@router.get("/presets")
async def list_presets(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """The chip row. Empty custom list -> the defaults, so a new install is
    immediately usable rather than showing a blank toolbar."""
    rows = (
        await session.exec(select(CommandPreset).order_by(CommandPreset.position))
    ).all()
    if not rows:
        return {"presets": DEFAULT_PRESETS, "custom": False}
    return {
        "presets": [{"label": r.label, "command": r.command} for r in rows],
        "custom": True,
    }


@router.put("/presets")
async def save_presets(
    payload: PresetsIn, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """Replace the whole list. Saved commands become runnable -- see
    `CommandPreset` for why that is the point rather than a loophole."""
    for row in (await session.exec(select(CommandPreset))).all():
        await session.delete(row)

    for i, item in enumerate(payload.presets):
        label = item.label.strip() or item.command.strip()[:24]
        command = item.command.strip()
        if not command:
            continue
        session.add(CommandPreset(label=label, command=command, position=i))

    await session.commit()
    rows = (
        await session.exec(select(CommandPreset).order_by(CommandPreset.position))
    ).all()
    log.info("command presets updated: %d entries", len(rows))
    return {
        "presets": [{"label": r.label, "command": r.command} for r in rows],
        "custom": bool(rows),
    }


@router.post("/presets/reset")
async def reset_presets(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    for row in (await session.exec(select(CommandPreset))).all():
        await session.delete(row)
    await session.commit()
    return {"presets": DEFAULT_PRESETS, "custom": False}


# ------------------------------------------------------------- pulling files
#: Streaming a pulled file back means holding it on disk here first; the same
#: ceiling as uploads keeps a stray `/dev/zero` from filling the volume.
MAX_DOWNLOAD_BYTES = MAX_UPLOAD_BYTES


def _check_remote_path(path: str) -> str:
    target = (path or "").strip()
    if not target:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "path must not be empty")
    if ".." in PurePosixPath(target).parts:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "path must not contain '..'")
    try:
        # Same rule as for commands: the control API must not become a way to
        # lift credentials off a machine it holds a stored login for.
        cmds.reject_if_sensitive(target)
    except CommandRejected as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return target


@router.get("/ls")
async def remote_ls(
    device_id: str,
    path: str = ".",
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """List a remote directory over SFTP, so files can be found before pulling."""
    target = _check_remote_path(path)
    device = (await _resolve_devices(session, [device_id]))[0]
    try:
        conn = await pool.get(device)
        entries = await cmds.sftp_list(conn, target)
    except SSHError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    except (OSError, asyncssh.Error) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{target}: {exc}") from exc
    return {"device_id": device_id, "path": target, "entries": entries}


@router.get("/fetch")
async def fetch_file(
    device_id: str,
    path: str,
    background: BackgroundTasks = None,  # type: ignore[assignment]
    session: AsyncSession = Depends(get_session),
) -> FileResponse:
    """Pull one file off a device and hand it to the browser as a download.

    A GET with the path in the query string, so the browser can be pointed
    straight at it once the token is attached.
    """
    target = _check_remote_path(path)
    device = (await _resolve_devices(session, [device_id]))[0]

    tmp_dir = Path(tempfile.mkdtemp(prefix="split-pull-"))
    local = tmp_dir / (PurePosixPath(target).name or "download.bin")
    try:
        conn = await pool.get(device)
        size = await cmds.sftp_get(conn, target, local)
    except SSHError as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    except IsADirectoryError as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except (OSError, asyncssh.Error) as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{target}: {exc}") from exc

    if size > MAX_DOWNLOAD_BYTES:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"{target} is {size / 1024**3:.1f} GiB; the limit is "
            f"{MAX_DOWNLOAD_BYTES // 1024**3} GiB",
        )

    bus.exec_line(device_id, f"⇩ pulled {target} ({size} bytes)", "stdout")
    # Cleaned up only after the response has been sent, or the file would be
    # deleted out from under the still-streaming download.
    if background is not None:
        background.add_task(shutil.rmtree, tmp_dir, ignore_errors=True)
    return FileResponse(local, filename=local.name, media_type="application/octet-stream")


# --------------------------------------------------------------- directories
@router.get("/directories")
async def list_directories(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    rows = (
        await session.exec(select(DirectoryPreset).order_by(DirectoryPreset.position))
    ).all()
    return {"directories": [{"label": r.label, "path": r.path} for r in rows]}


@router.put("/directories")
async def save_directories(
    payload: DirectoriesIn, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """Replace the saved working directories.

    Grants nothing on its own: a path only ever reaches `cd`, and whatever runs
    inside it goes through the same validation as any other command.
    """
    for row in (await session.exec(select(DirectoryPreset))).all():
        await session.delete(row)

    for i, item in enumerate(payload.directories):
        path = item.path.strip()
        if not path:
            continue
        # Default the chip's label to the last path segment: `.../split_infer`
        # is what distinguishes it, and the full path is on the tooltip.
        label = item.label.strip() or PurePosixPath(path).name or path
        session.add(DirectoryPreset(label=label, path=path, position=i))

    await session.commit()
    rows = (
        await session.exec(select(DirectoryPreset).order_by(DirectoryPreset.position))
    ).all()
    return {"directories": [{"label": r.label, "path": r.path} for r in rows]}


@router.get("/allowed-commands")
async def allowed_commands() -> dict[str, Any]:
    return {
        "prefixes": list(ALLOWED_PREFIXES) + list(ALLOWED_MODULE_PREFIXES),
        "python_inline": list(cmds.PYTHON_INLINE_PREFIXES),
        #: `python3 Server.py` and friends -- a pattern rather than a prefix,
        #: so the UI can explain it instead of listing every script name.
        "python_script": cmds.PYTHON_SCRIPT_RE.pattern,
        "destructive": list(cmds.DESTRUCTIVE_PREFIXES),
        "broker_ip_token": cmds.BROKER_IP_TOKEN,
        "broker_ip": server_state.host,
        "unsafe_enabled": settings.allow_unsafe_commands,
        "detach_after_s": settings.exec_detach_after,
    }


# ----------------------------------------------------------------------- scp
@router.post("/scp")
async def scp(
    file: UploadFile = File(...),
    device_ids: str = Form(...),
    remote_path: str = Form(...),
    verify: bool = Form(default=True),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Push one uploaded file to many devices.

    `device_ids` is comma-separated (multipart has no native list type).
    If `remote_path` ends in `/`, the uploaded filename is appended.
    """
    ids = [i.strip() for i in device_ids.split(",") if i.strip()]
    devices = await _resolve_devices(session, ids)

    target = remote_path.strip()
    if not target:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "remote_path must not be empty")
    if not target.startswith("/"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "remote_path must be absolute")
    if target.endswith("/"):
        target += Path(file.filename or "upload.bin").name
    if ".." in PurePosixPath(target).parts:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "remote_path must not contain '..'")

    # Spool to disk: shards are hundreds of MB and must not sit in memory.
    tmp_dir = Path(tempfile.mkdtemp(prefix="split-scp-"))
    local = tmp_dir / Path(file.filename or "upload.bin").name
    size = 0
    try:
        with local.open("wb") as out:
            while chunk := await file.read(1 << 20):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        f"upload exceeds {MAX_UPLOAD_BYTES // 1024**3} GiB",
                    )
                out.write(chunk)

        bus.exec_line(
            "", f"⇪ scp {local.name} → {len(devices)} host(s):{target}", "meta"
        )
        results = await cmds.scp_put_many(pool, devices, local, target, verify=verify)
        return {
            "local_name": local.name,
            "bytes": size,
            "remote_path": target,
            "results": [r.to_dict() for r in results],
            "ok": all(r.ok for r in results),
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
