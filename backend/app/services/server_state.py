"""Process-level cache of the configured broker/server host.

`ssh/commands.py::fan_out` substitutes `$BROKER_IP` but has no DB session, so
the singleton `ServerConfig.host` is mirrored here. Refreshed on startup and
whenever `POST /server/config` writes.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ServerState:
    host: str = ""
    port: int = 5672
    api_port: int = 8000
    username: str = "guest"
    has_credentials: bool = False
    #: Result of the last /server/test: off | on | error
    status: str = "off"

    def update(
        self,
        *,
        host: str,
        port: int,
        api_port: int,
        username: str,
        has_credentials: bool,
    ) -> None:
        self.host = host or ""
        self.port = port
        self.api_port = api_port
        self.username = username or ""
        self.has_credentials = has_credentials


#: Process-wide singleton.
server_state = ServerState()
