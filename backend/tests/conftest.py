"""Test fixtures. Each test module gets its own throwaway SQLite file and
secrets dir, and the broker is never contacted.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

TOKEN = "test-token"

# Settings are read at import time, so the environment must be set before any
# `app.*` module is imported.
_TMP = Path(tempfile.mkdtemp(prefix="split-tests-"))
os.environ.update(
    API_TOKEN=TOKEN,
    DATABASE_URL=f"sqlite+aiosqlite:///{(_TMP / 'test.db').as_posix()}",
    SECRETS_DIR=str(_TMP / "secrets"),
    SHARDS_DIR=str(_TMP / "shards"),
    REPORTS_DIR=str(_TMP / "reports"),
    AUTORUN_DIR=str(_TMP / "autorun"),
    # No test may ever reach out to Telegram. Tests that exercise notification
    # content flip this back on with a stubbed transport.
    NOTIFY_ENABLED="false",
    TELEGRAM_BOT_TOKEN="",
    TELEGRAM_CHAT_ID="",
    CORS_ORIGINS="*",
    MAX_MESSAGE_MB="15",
    ALLOW_UNSAFE_COMMANDS="false",
    METRICS_BROADCAST_HZ="20",
)


@pytest.fixture(scope="session")
def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
def client():
    """TestClient with lifespan run, a fresh database, and no broker.

    The database file is removed between tests: shared state made the suite
    order-dependent, which hid real failures behind whatever ran first.
    """
    import asyncio
    from unittest.mock import AsyncMock, patch

    from fastapi.testclient import TestClient

    from app.db import engine
    from app.inference.orchestrator import orchestrator
    from app.main import app
    from app.services.metrics_bus import bus

    # Drop the previous test's schema; lifespan's init_db recreates it.
    asyncio.run(engine.dispose())
    db_file = _TMP / "test.db"
    for path in (db_file, Path(f"{db_file}-wal"), Path(f"{db_file}-shm")):
        path.unlink(missing_ok=True)

    bus.clear_metrics()
    bus._ssh_status.clear()
    bus._history.clear()
    orchestrator._runs.clear()
    orchestrator._windows.clear()

    with patch("app.main.broker.connect", new=AsyncMock()), patch(
        "app.main.broker.close", new=AsyncMock()
    ):
        with TestClient(app) as c:
            yield c


@pytest.fixture()
def ui_export() -> dict:
    """The UI's `exportJson()` payload for the default scenario
    (`defaultStages()` + default `config`)."""
    return {
        "model": "yolov11n",
        "config": {
            "clustering": True,
            "numClusters": 2,
            "autoBalance": "power",
            "manualEnabled": False,
            "manualSplit": 5,
            "modelName": "yolov11n",
        },
        "stages": [
            {
                "id": "s1", "kind": "Edge", "name": "Edge",
                "devices": [
                    {"id": "dA", "name": "Jetson-A", "gflops": 472, "bw": 12, "lat": 6, "cluster": 1},
                    {"id": "dB", "name": "Jetson-B", "gflops": 472, "bw": 12, "lat": 6, "cluster": 1},
                    {"id": "dC", "name": "Jetson-C", "gflops": 384, "bw": 10, "lat": 8, "cluster": 2},
                ],
            },
            {
                "id": "s2", "kind": "Cloud", "name": "Cloud",
                "devices": [
                    {"id": "dG1", "name": "GPU-1", "gflops": 9800, "bw": 125, "lat": 2, "cluster": 1},
                    {"id": "dG2", "name": "GPU-2", "gflops": 9800, "bw": 125, "lat": 2, "cluster": 2},
                ],
            },
        ],
        "clusters": [],
    }
