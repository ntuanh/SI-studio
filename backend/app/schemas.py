"""Pydantic v2 request/response models.

Wire names deliberately match the UI's state (guide §8): a device serializes
with `bw` / `lat` / `cluster` aliases alongside the canonical
`bandwidth_mb_s` / `latency_ms` / `cluster_id`, so the existing front-end needs
no field renaming. Passwords and key material are never serialized outward.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DeviceKind = Literal["Edge", "Fog", "Cloud", "Custom"]
AuthMethod = Literal["key", "password"]
Role = Literal["head", "tail", "auto"]
SshStatus = Literal["off", "connecting", "on", "error"]


class _Base(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


# ------------------------------------------------------------------- devices
class DeviceIn(_Base):
    id: str | None = None  # UI-generated id; server mints one when absent
    name: str
    kind: DeviceKind = "Edge"
    cluster_id: int = Field(default=1, validation_alias="cluster")

    host: str = ""
    port: int = 22
    username: str = "root"
    auth_method: AuthMethod = "key"
    key_ref: str = ""
    #: Write-only. Stored in the secret store, never read back.
    password: str | None = None

    gflops: float = 0.0
    bandwidth_mb_s: float = Field(default=0.0, validation_alias="bw")
    latency_ms: float = Field(default=0.0, validation_alias="lat")

    stage_id: str = ""
    stage_name: str = ""
    role: Role = "auto"

    @field_validator("port")
    @classmethod
    def _port_range(cls, v: int) -> int:
        if not 1 <= v <= 65535:
            raise ValueError("port must be in 1..65535")
        return v

    @field_validator("cluster_id")
    @classmethod
    def _cluster_positive(cls, v: int) -> int:
        return max(1, v)


class DevicePatch(_Base):
    name: str | None = None
    kind: DeviceKind | None = None
    cluster_id: int | None = Field(default=None, validation_alias="cluster")
    host: str | None = None
    port: int | None = None
    username: str | None = None
    auth_method: AuthMethod | None = None
    key_ref: str | None = None
    password: str | None = None
    gflops: float | None = None
    bandwidth_mb_s: float | None = Field(default=None, validation_alias="bw")
    latency_ms: float | None = Field(default=None, validation_alias="lat")
    stage_id: str | None = None
    stage_name: str | None = None
    role: Role | None = None


class DeviceOut(_Base):
    id: str
    name: str
    kind: str
    cluster_id: int
    host: str
    port: int
    username: str
    auth_method: str
    key_ref: str
    gflops: float
    bandwidth_mb_s: float
    latency_ms: float
    stage_id: str
    stage_name: str
    role: str
    side: str
    #: UI aliases -- emitted so the front-end can bind either spelling.
    bw: float
    lat: float
    cluster: int
    #: Derived, never the secret itself.
    has_password: bool
    ssh_status: str
    probed_at: datetime | None = None
    probe_info: dict[str, Any] = Field(default_factory=dict)


# ------------------------------------------------------------------ clusters
class ClusterIn(_Base):
    id: int
    model_name: str | None = None
    batch_size: int = 32
    num_bit: int = 8
    cut_layer: int | None = Field(default=None, validation_alias="splitOverride")
    queue_name: str | None = None

    @field_validator("num_bit")
    @classmethod
    def _bits(cls, v: int) -> int:
        if not 1 <= v <= 32:
            raise ValueError("num_bit must be in 1..32")
        return v


class ClusterPatch(_Base):
    model_name: str | None = None
    batch_size: int | None = None
    num_bit: int | None = None
    cut_layer: int | None = Field(default=None, validation_alias="splitOverride")
    queue_name: str | None = None


class ClusterOut(_Base):
    id: int
    queue_name: str
    model_name: str
    batch_size: int
    num_bit: int
    cut_layer: int | None
    edge_devices: list[str] = Field(default_factory=list)
    cloud_devices: list[str] = Field(default_factory=list)
    live: bool = False


# -------------------------------------------------------------------- config
class GlobalConfigIn(_Base):
    clustering: bool | None = None
    num_clusters: int | None = Field(default=None, validation_alias="numClusters")
    auto_balance: Literal["power", "latency"] | None = Field(
        default=None, validation_alias="autoBalance"
    )
    manual_enabled: bool | None = Field(default=None, validation_alias="manualEnabled")
    manual_split: int | None = Field(default=None, validation_alias="manualSplit")
    model_name: str | None = Field(default=None, validation_alias="modelName")


class GlobalConfigOut(_Base):
    clustering: bool
    num_clusters: int
    auto_balance: str
    manual_enabled: bool
    manual_split: int
    model_name: str
    max_message_mb: float


# ------------------------------------------------------------------- control
class ConnectCredential(_Base):
    """Per-device override sent from the UI's `state.ssh.conn[id]` form."""

    device_id: str
    host: str | None = Field(default=None, validation_alias="ip")
    port: int | None = None
    username: str | None = Field(default=None, validation_alias="user")
    password: str | None = None
    auth_method: AuthMethod | None = None
    key_ref: str | None = None
    #: Persist the password to the secret store instead of using it once.
    remember: bool = True


class ConnectRequest(_Base):
    device_ids: list[str] = Field(default_factory=list)
    credentials: list[ConnectCredential] = Field(default_factory=list)


class DisconnectRequest(_Base):
    device_ids: list[str] | None = None  # None -> all open sessions


class ExecRequest(_Base):
    device_ids: list[str]
    command: str
    #: Hard deadline. `None` means *no limit* -- a measurement run takes as
    #: long as it takes, and its output streams to the console throughout.
    timeout: float | None = None
    #: Required for destructive presets (reboot, systemctl stop|restart).
    confirm: bool = False
    #: Run from here. Each command gets its own shell, so a bare `cd` cannot
    #: persist -- the directory has to travel with every command.
    cwd: str | None = None
    #: Seconds to wait before answering and leaving the command running.
    #: Defaults to EXEC_DETACH_AFTER.
    detach_after: float | None = None


class ExecStopRequest(_Base):
    """Ctrl-C. Either a specific job, or everything on these devices."""

    job_id: str | None = None
    device_ids: list[str] = Field(default_factory=list)


class PresetIn(_Base):
    label: str = ""
    command: str


class PresetsIn(_Base):
    presets: list[PresetIn] = Field(default_factory=list)


class DirectoryIn(_Base):
    label: str = ""
    path: str


class DirectoriesIn(_Base):
    directories: list[DirectoryIn] = Field(default_factory=list)


class ExecResponse(_Base):
    command: str
    results: list[dict[str, Any]]
    ok: bool
    #: Commands still running when this answered. Their output keeps arriving
    #: as `exec_line` frames; stop them with `POST /control/exec/stop`.
    running: list[dict[str, Any]] = Field(default_factory=list)


# ----------------------------------------------------------- result analysis
class AnalyzeRequest(_Base):
    """Chart a finished run's result directory (see routers/reports.py)."""

    device_id: str
    path: str
    #: Names the report alongside the timestamp, so a saved folder reads as
    #: `2807-1432_cut6-8bit` rather than a bare date.
    case_name: str = ""
    #: The static slice of the run to analyse, as percentages of its readings:
    #: `5`–`90` keeps batches 5 through 90 of a hundred. `0`–`100` is the whole
    #: run, which is what every report was before this existed. See
    #: `reports/window.py` for what a window can and cannot narrow.
    window_start: float = 0.0
    window_end: float = 100.0

    @field_validator("window_start", "window_end")
    @classmethod
    def _a_percentage(cls, value: float) -> float:
        if not 0.0 <= value <= 100.0:
            raise ValueError("window bounds are percentages, so 0–100")
        return value

    @model_validator(mode="after")
    def _forward(self) -> "AnalyzeRequest":
        # An inverted or empty window is rejected rather than silently widened:
        # it is a typo in the box, and analysing the whole run instead would
        # answer a question nobody asked.
        if self.window_start >= self.window_end:
            raise ValueError("the window's start must come before its end")
        return self


class NotesIn(_Base):
    """The operator's short review: one note per chart, plus an overall."""

    notes: dict[str, str] = Field(default_factory=dict)
    review: str | None = None


class ChartViewIn(_Base):
    """What the config panel changed about one chart.

    Empty strings mean "back to the default", which is how Reset is expressed --
    `None` would be ambiguous with "not sent".
    """

    title: str = ""
    xlabel: str = ""
    ylabel: str = ""
    #: Series keys to leave out. Hiding every series is ignored downstream: an
    #: empty frame with a title is not a chart.
    hidden: list[str] = Field(default_factory=list)


class ViewsIn(_Base):
    """One entry per chart the operator has configured, keyed by chart id.

    The charts are server-rendered PNGs, so applying this re-draws the report.
    The whole map is sent rather than a diff: it is small, and it means the
    stored state and the panel can never disagree about what is hidden.
    """

    views: dict[str, ChartViewIn] = Field(default_factory=dict)


class StatusOut(_Base):
    statuses: dict[str, str]


# ------------------------------------------------------------- server config
class ServerConfigIn(_Base):
    """The Control tab's broker/server card. Both passwords are write-only.

    `port`/`username`/`password` are the **AMQP** login; `ssh_*` is the SSH
    login to the same host. They are separate because they nearly always are
    in practice (`guest` for RabbitMQ, a real account for SSH).
    """

    host: str = Field(validation_alias="ip")
    port: int = 5672
    api_port: int | None = None
    username: str = Field(default="guest", validation_alias="user")
    password: str | None = None
    #: Where RabbitMQ is, when that is not the SSH host. Empty -> BROKER_URL.
    amqp_host: str | None = None

    # --- SSH into the same host ---
    ssh_port: int | None = None
    ssh_username: str | None = Field(default=None, validation_alias="ssh_user")
    ssh_password: str | None = None
    #: Tunnel every device connection through this host (ProxyJump).
    jump_enabled: bool | None = None

    @field_validator("port")
    @classmethod
    def _amqp_port(cls, v: int) -> int:
        if not 1 <= v <= 65535:
            raise ValueError("port must be in 1..65535")
        return v

    @field_validator("ssh_port")
    @classmethod
    def _ssh_port(cls, v: int | None) -> int | None:
        if v is not None and not 1 <= v <= 65535:
            raise ValueError("ssh_port must be in 1..65535")
        return v

    @field_validator("api_port")
    @classmethod
    def _api_port(cls, v: int | None) -> int | None:
        if v is not None and not 1 <= v <= 65535:
            raise ValueError("api_port must be in 1..65535")
        return v

    @field_validator("host")
    @classmethod
    def _host_present(cls, v: str) -> str:
        if not (v or "").strip():
            raise ValueError("host (ip) must not be empty")
        return v.strip()


class ServerConfigOut(_Base):
    """Never carries either password -- only whether one is on file."""

    host: str
    port: int
    api_port: int
    username: str
    has_credentials: bool
    #: UI aliases, mirroring the device serializer.
    ip: str
    user: str
    status: str = "off"
    auth_ok: bool = False
    updated_at: datetime | None = None

    # --- SSH ---
    ssh_port: int = 22
    ssh_username: str = ""
    ssh_user: str = ""  # UI alias
    has_ssh_credentials: bool = False
    ssh_status: str = "off"
    jump_enabled: bool = False

    # --- broker, which may live on another machine entirely ---
    amqp_host: str = ""  # as configured ("" = inherit)
    amqp_host_resolved: str = ""  # what will actually be dialled


class ServerTestOut(_Base):
    ok: bool
    rabbitmq_version: str = ""
    product: str = ""
    cluster_name: str = ""
    platform: str = ""
    api: Literal["up", "down"] = "down"
    api_detail: str = ""
    broker_error: str = ""
    host: str = ""
    checked_at: datetime

    # --- SSH leg ---
    #: "ok" when the login succeeded, "skipped" when no SSH user is configured.
    ssh: Literal["ok", "failed", "skipped"] = "skipped"
    ssh_error: str = ""
    ssh_banner: str = ""  # `uname -a` from the server, proof it really ran


# ----------------------------------------------------------------------- run
class DeployRequest(_Base):
    cluster_id: int | None = Field(default=None, validation_alias="cluster")
    install_deps: bool = False
    head_shard: str | None = None
    tail_shard: str | None = None


class StartRequest(_Base):
    cluster_id: int | None = Field(default=None, validation_alias="cluster")
    max_frames: int | None = None


class StopRequest(_Base):
    cluster_id: int | None = Field(default=None, validation_alias="cluster")
    drain: bool = True


# ------------------------------------------------------------------- metrics
class MetricsOut(_Base):
    clusters: list[dict[str, Any]]
    aggregate_fps: float
    live_clusters: list[int]
    generated_at: datetime


# ---------------------------------------------------------------------- seed
class SeedRequest(_Base):
    """Import the UI's exported JSON (`exportJson()` output) verbatim.

    Shape: {model, config: {...}, stages: [{id,kind,name,devices:[...]}], clusters: [...]}
    """

    model: str | None = None
    config: dict[str, Any] | None = None
    stages: list[dict[str, Any]] = Field(default_factory=list)
    clusters: list[dict[str, Any]] = Field(default_factory=list)
    uploaded_model: dict[str, Any] | None = Field(default=None, validation_alias="uploadedModel")
    cluster_cfg: dict[str, Any] | None = Field(default=None, validation_alias="clusterCfg")
    #: Wipe existing devices/clusters first.
    replace: bool = True
    #: Optional connection defaults applied to imported devices.
    default_username: str = "root"
    default_port: int = 22
    default_auth_method: AuthMethod = "key"
    default_key_ref: str = ""


class SeedResponse(_Base):
    devices: int
    clusters: int
    models: int
    config: GlobalConfigOut


# ---------------------------------------------------------------------- keys
class KeyIn(_Base):
    id: str
    label: str = ""
    private_key: str
    passphrase: str | None = None


class KeyOut(_Base):
    id: str
    label: str
    fingerprint: str
    has_passphrase: bool
    created_at: datetime


# --------------------------------------------------------------------- probe
class ProbeOut(_Base):
    device_id: str
    ok: bool
    gflops: float | None = None
    bandwidth_mb_s: float | None = None
    latency_ms: float | None = None
    info: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    error: str = ""


# ------------------------------------------------------------------- measure
class MeasureOut(_Base):
    """One device's automatic measurement (`services/measure.py`)."""

    device_id: str
    device_name: str = ""
    ok: bool
    gflops: float | None = None
    #: What was written to the spec field -- `bandwidth_basis` decides which of
    #: the two figures below that is.
    bandwidth_mb_s: float | None = None
    latency_ms: float | None = None
    #: The link with nothing else on it.
    bandwidth_solo_mb_s: float | None = None
    #: The same test with every other device transferring at the same time.
    bandwidth_shared_mb_s: float | None = None
    #: shared / solo. ~1.0 means this device has its uplink to itself.
    contention_ratio: float | None = None
    #: Which method produced each number, e.g. {"gflops": "conv-fp32"}.
    sources: dict[str, str] = Field(default_factory=dict)
    info: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    error: str = ""


class FleetMeasureOut(_Base):
    results: list[MeasureOut] = Field(default_factory=list)
    #: Device/measured counts, whether the contention pass ran, and the
    #: aggregate throughput the fleet reached while sharing the link.
    summary: dict[str, Any] = Field(default_factory=dict)
    applied: bool = False
    bandwidth_basis: str = "shared"
