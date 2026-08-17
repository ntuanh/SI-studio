"""SQLModel tables. Field names mirror the UI's device/cluster state (§4).

UI -> backend name mapping (kept explicit so the UI needs no refactor):
    gflops  -> gflops
    bw      -> bandwidth_mb_s
    lat     -> latency_ms
    cluster -> cluster_id
The schemas layer (`schemas.py`) does the aliasing on the wire.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, ClassVar

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Device(SQLModel, table=True):
    __tablename__ = "device"

    id: str = Field(primary_key=True)  # matches the UI device id (e.g. "dk3f9")
    name: str
    kind: str = "Edge"  # Edge | Fog | Cloud | Custom
    cluster_id: int = 1

    # --- connection ---
    host: str = ""  # ip or dns
    port: int = 22
    username: str = "root"
    auth_method: str = "key"  # key | password
    key_ref: str = ""  # -> secrets/<key_ref>.pem  (never returned by the API)

    # --- specs (drive the simulation math) ---
    gflops: float = 0.0
    bandwidth_mb_s: float = 0.0
    latency_ms: float = 0.0

    # --- topology ---
    stage_id: str = ""
    stage_name: str = ""
    role: str = "auto"  # head | tail | auto (derived from kind)

    probed_at: datetime | None = None
    probe_info: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=_utcnow)

    @property
    def side(self) -> str:
        """UI rule (buildClusters): only kind == 'Cloud' is the tail side."""
        return "cloud" if self.kind == "Cloud" else "edge"

    @property
    def resolved_role(self) -> str:
        if self.role in ("head", "tail"):
            return self.role
        return "tail" if self.side == "cloud" else "head"


class Cluster(SQLModel, table=True):
    __tablename__ = "cluster"

    id: int = Field(primary_key=True)
    queue_name: str = ""  # intermediate_queue_<id>
    model_name: str = "yolov11n"
    batch_size: int = 32
    num_bit: int = 8  # compression bit width
    cut_layer: int | None = None  # manual override; None -> auto-select
    created_at: datetime = Field(default_factory=_utcnow)

    def ensure_queue_name(self) -> str:
        if not self.queue_name:
            self.queue_name = f"intermediate_queue_{self.id}"
        return self.queue_name


class Run(SQLModel, table=True):
    __tablename__ = "run"

    id: str = Field(primary_key=True)
    cluster_id: int
    status: str = "pending"  # pending | deploying | running | stopped | error
    model_name: str = ""
    cut_layer: int | None = None
    num_bit: int = 8
    batch_size: int = 32
    started_at: datetime = Field(default_factory=_utcnow)
    stopped_at: datetime | None = None
    detail: str = ""
    device_pids: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class KeyRef(SQLModel, table=True):
    """Registry of SSH keys on disk. The private key material never leaves the box."""

    __tablename__ = "keyref"

    id: str = Field(primary_key=True)  # <key_ref>  -> secrets/<key_ref>.pem
    label: str = ""
    fingerprint: str = ""
    has_passphrase: bool = False
    created_at: datetime = Field(default_factory=_utcnow)


class ModelDef(SQLModel, table=True):
    """Custom / uploaded model layer table (the UI's `state.uploadedModel`)."""

    __tablename__ = "modeldef"

    name: str = Field(primary_key=True)
    label: str = ""
    layers: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=_utcnow)


class ServerConfig(SQLModel, table=True):
    """Singleton: the control server the Control tab's top card configures.

    Two independent things that happen to share one card:

    * **The control server** (`host`, `ssh_*`) -- the machine you SSH into, and
      optionally the jump host the devices are reached through.
    * **The broker** (`amqp_host`, `port`, `username`) -- where RabbitMQ runs,
      which is frequently a *different* machine.

    They were one host originally and that was wrong: a gateway you SSH through
    and a broker your devices publish to have no reason to be the same box. The
    SSH and AMQP logins are likewise separate, because they nearly always are
    in practice (`dai` vs `guest`).

    Neither password is stored here: both live in the encrypted secret store
    and the row keeps only a ref.
    """

    __tablename__ = "serverconfig"

    id: int = Field(default=1, primary_key=True)
    host: str = ""  # UI: ip -- the machine you SSH into
    port: int = 5672  # UI: port (AMQP)
    api_port: int = 8000  # control API
    username: str = "guest"  # UI: user (AMQP)
    password_ref: str | None = None  # -> secret store id, NEVER plaintext
    #: Where RabbitMQ actually is. Often *not* the SSH server -- a broker on
    #: the control-plane host talking to devices through a gateway is a normal
    #: shape. Empty means "wherever BROKER_URL points", i.e. the broker this
    #: backend already connects to.
    amqp_host: str = ""

    # --- SSH into this same host ---
    ssh_port: int = 22
    ssh_username: str = ""
    ssh_password_ref: str | None = None
    #: Route every device's SSH through this host (asyncssh `tunnel=`, i.e.
    #: OpenSSH's ProxyJump). For labs where the devices sit on a private
    #: network only the server can reach.
    jump_enabled: bool = False

    updated_at: datetime = Field(default_factory=_utcnow)

    #: Stable secret-store refs for the singleton row.
    SECRET_REF: ClassVar[str] = "server-config"
    #: Must equal `ssh.gateway.SERVER_DEVICE_ID`: the pool looks a target's
    #: password up by target id, so storing the server's SSH password under
    #: anything else would leave the pool unable to find it. `gateway` asserts
    #: the two agree at import time.
    SSH_SECRET_REF: ClassVar[str] = "__server__"

    @property
    def api_base_url(self) -> str:
        """The control API is *this* process, so the probe is a self-check.

        It used to be `http://{host}:{api_port}`, which only made sense while
        `host` also meant "the backend". Now that `host` is the SSH gateway --
        a machine that generally runs no control API -- probing it would report
        `api: down` for a perfectly healthy deployment.
        """
        return f"http://127.0.0.1:{self.api_port}"

    # The AMQP URL needs the stored password, so it is built in
    # `routers/server.py::_amqp_url` rather than here -- the model layer has no
    # business reaching into the secret store.


class CommandPreset(SQLModel, table=True):
    """An operator's own frequently-used command, shown as a chip.

    Saving one is an explicit statement of intent, so a saved preset is also
    **allowed to run** -- that is what makes the feature useful rather than
    decorative. The built-in allow-list still guards commands typed ad hoc, and
    the destructive-confirmation check still applies to presets.
    """

    __tablename__ = "commandpreset"

    id: int | None = Field(default=None, primary_key=True)
    label: str
    command: str
    position: int = 0


class DirectoryPreset(SQLModel, table=True):
    """A frequently-used working directory, shown as a chip.

    Unlike a command preset this grants nothing: the path is only ever passed
    to `cd`, and the command run inside it is validated exactly as before.
    """

    __tablename__ = "directorypreset"

    id: int | None = Field(default=None, primary_key=True)
    label: str
    path: str
    position: int = 0


class QueueProject(SQLModel, table=True):
    """One project in the Progress tab's run-everything queue.

    The whole feature exists because running a project by hand is three
    identical gestures -- select the server and Run, select the edges and Run,
    select the clouds and Run -- and the *only* thing that differs between one
    project and the next is which directory those commands run in. So a project
    is a directory with a name on it, and nothing else is required.

    `overrides` is the escape hatch for the project that does differ: a map of
    target -> command, keyed by `__server__` or a stage id, empty for the normal
    case. It is how `--device cpu` gets onto one project's clients without
    forking the shared preset every other project uses.
    """

    __tablename__ = "queueproject"

    id: int | None = Field(default=None, primary_key=True)
    name: str
    path: str
    enabled: bool = True
    position: int = 0
    #: Typical runtime in seconds, from the last few runs or from the operator.
    #: Drives the elapsed-vs-expected bar; 0 means "no estimate", which shows
    #: no such bar rather than a made-up one.
    expected_s: int = 0
    overrides: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class AuditLog(SQLModel, table=True):
    """Record of destructive commands run through /control/exec."""

    __tablename__ = "auditlog"

    id: str = Field(primary_key=True)
    action: str  # exec | exec_denied
    command: str
    device_ids: str = ""  # comma-separated
    confirmed: bool = False
    outcome: str = ""
    at: datetime = Field(default_factory=_utcnow)


class GlobalConfig(SQLModel, table=True):
    """Single-row mirror of the UI's `state.config` so /metrics/latest can
    reproduce the simulator's cut choice exactly."""

    __tablename__ = "globalconfig"

    id: int = Field(default=1, primary_key=True)
    clustering: bool = True
    num_clusters: int = 2
    auto_balance: str = "power"  # power | latency
    manual_enabled: bool = False
    manual_split: int = 5
    model_name: str = "yolov11n"
