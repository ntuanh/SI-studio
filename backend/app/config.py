"""Settings, loaded from environment / .env (pydantic-settings)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- auth (single-token stub; §1 non-goals says no auth UI) ---
    api_token: str = "dev-token-change-me"

    # --- http ---
    cors_origins: str = "*"
    #: Pre-fill the API token in /docs so you don't have to click Authorize.
    #: Only ever applied to requests from loopback (see routers wiring), so
    #: binding to 0.0.0.0 does not hand the token to remote visitors.
    docs_autofill_token: bool = True
    #: Same rule for the bundled UI at `/`: local browsers get the token in
    #: `runtime-config.js`, remote ones are asked to paste it.
    web_autofill_token: bool = True

    # --- persistence ---
    database_url: str = "sqlite+aiosqlite:///./split_inference.db"

    # --- broker ---
    broker_url: str = "amqp://guest:guest@localhost:5672/"
    device_broker_url: str = ""
    rabbitmq_mgmt_url: str = "http://localhost:15672"
    rabbitmq_mgmt_user: str = "guest"
    rabbitmq_mgmt_password: str = "guest"
    rabbitmq_vhost: str = "/"

    # --- filesystem ---
    secrets_dir: str = "./secrets"
    shards_dir: str = "./shards"
    #: Saved chart reports: one directory per report, holding the PNGs, the
    #: manifest and the operator's notes. Plain files rather than DB rows so a
    #: report stays readable (and copyable) without this service running.
    reports_dir: str = "./reports"

    # --- remote layout ---
    remote_root: str = "/opt/split-inference"
    remote_python: str = "python3"
    #: Where `agent/gflops_bench.py` is copied to on each device. Relative, so
    #: it lands under the login user's home and needs no root -- the benchmark
    #: is a read-only measurement and should not require write access to
    #: `remote_root`, which the deployment owns.
    remote_bench_dir: str = "ntuanh"

    # --- ssh ---
    fanout_concurrency: int = 8
    ssh_connect_timeout: int = 10
    #: Applies to the internal one-shot probes (`uname -a`, agent launch,
    #: `sha256sum`). Operator commands from the Control tab are *not* capped by
    #: it -- see `exec_detach_after`.
    ssh_command_timeout: int = 120
    #: How long `/control/exec` waits for a command before answering and
    #: leaving it running. Short commands finish inside the window and are
    #: reported inline; a long one (`python3 Server.py`) keeps going with its
    #: output still streaming to the console, and is stopped from the same box
    #: with `^C`. There is deliberately no ceiling on the run itself: a
    #: measurement run takes as long as it takes, and the previous 120 s cap
    #: killed it mid-flight before it could write its result logs.
    exec_detach_after: float = 5.0
    allow_unsafe_commands: bool = False

    # --- simulation parity (UI prop `maxMessageMb`) ---
    max_message_mb: float = 15.0

    # --- metrics ---
    metrics_window: int = 60
    metrics_broadcast_hz: float = 2.0

    # ------------------------------------------------------------------
    @property
    def cors_origin_list(self) -> list[str]:
        raw = (self.cors_origins or "").strip()
        if raw in ("", "*"):
            return ["*"]
        return [o.strip() for o in raw.split(",") if o.strip()]

    @property
    def secrets_path(self) -> Path:
        return self._abs(self.secrets_dir)

    @property
    def shards_path(self) -> Path:
        return self._abs(self.shards_dir)

    @property
    def reports_path(self) -> Path:
        return self._abs(self.reports_dir)

    @property
    def agent_broker_url(self) -> str:
        """Broker URL handed to the agents running on the devices."""
        return self.device_broker_url.strip() or self.broker_url

    def _abs(self, value: str) -> Path:
        p = Path(value)
        return p if p.is_absolute() else (BACKEND_ROOT / p).resolve()


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.secrets_path.mkdir(parents=True, exist_ok=True)
    s.shards_path.mkdir(parents=True, exist_ok=True)
    s.reports_path.mkdir(parents=True, exist_ok=True)
    return s


settings = get_settings()
