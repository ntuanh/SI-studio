"""FastAPI app: CORS, routers, lifespan, and the /ws/stream WebSocket."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import Depends, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse

from .auth import authorize_websocket, require_token
from .config import settings
from .db import dispose_db, init_db
from .inference.broker import broker
from .inference.orchestrator import orchestrator
from .routers import clusters, control, devices, metrics, reports, run, server, web
from .services.metrics_bus import bus
from .ssh import gateway
from .ssh.pool import pool

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("split_inference")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await init_db()
    log.info("database ready (%s)", settings.database_url)

    # Load the configured broker host into the process cache so `$BROKER_IP`
    # resolves on the first fan-out, before any /server/config POST.
    from .db import SessionFactory

    try:
        async with SessionFactory() as session:
            await server.refresh_server_state(session)
    except Exception:
        log.exception("could not load server config; $BROKER_IP stays unset")

    # With `jump_enabled` on the server card, every device connection is
    # tunnelled through the control server. Installed as a callback so the ssh
    # layer never imports the DB or a router.
    pool.set_jump_provider(gateway.jump_connection)

    # The broker is optional at boot: the UI's Simulate path and the whole
    # Control tab work without RabbitMQ. /run/start reconnects on demand.
    try:
        await broker.connect()
    except Exception as exc:  # noqa: BLE001 - degraded start is intentional
        log.warning("broker unavailable at startup (%s); will retry on /run/start", exc)

    try:
        yield
    finally:
        log.info("shutting down: stopping runs, closing SSH + broker")
        from .db import SessionFactory

        try:
            async with SessionFactory() as session:
                await orchestrator.stop_all(session)
        except Exception:
            log.exception("failed to stop runs cleanly")
        await pool.aclose()
        await broker.close()
        await dispose_db()


app = FastAPI(
    title="Split Inference Control Plane",
    version="1.0.0",
    summary="SSH fan-out, shard deployment, and live split-inference metrics for "
    "the Split Inference Simulation Platform UI.",
    lifespan=lifespan,
    # Swagger is served by `docs()` below so it can pre-authorize itself.
    docs_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,  # bearer token, not cookies -- keeps "*" usable
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(devices.router)
app.include_router(control.router)
app.include_router(server.router)
app.include_router(clusters.router)
app.include_router(run.router)
app.include_router(metrics.router)
app.include_router(reports.router)


# ------------------------------------------------------------------------ docs
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"}


def _is_loopback(request: Request) -> bool:
    """True only for a direct connection from this machine.

    Deliberately ignores X-Forwarded-For: a proxied request is not local, and
    trusting that header would let a remote caller claim it is.
    """
    client = request.client
    return bool(client and client.host in LOOPBACK_HOSTS)


SWAGGER_JS = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"
SWAGGER_CSS = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css"


def _js_literal(value: str) -> str:
    """JSON-encode for embedding in a <script>, neutralising `</script>`."""
    return json.dumps(value).replace("<", "\\u003c").replace("&", "\\u0026")


@app.get("/docs", include_in_schema=False)
async def docs(request: Request) -> HTMLResponse:
    """Swagger UI that authorizes itself when opened locally.

    The token is injected only for loopback requests, so running with
    `--host 0.0.0.0` does not expose it to anyone who loads /docs. Remote
    viewers get the stock page and the usual Authorize button.

    The page is built here rather than via `get_swagger_ui_html` because the
    stock output declares `const ui = SwaggerUIBundle(...)`. A top-level `const`
    lives in script scope and never becomes a property of `window`, so
    `window.ui.preauthorizeApiKey(...)` can never be reached from an injected
    script. Owning the init lets us both expose `ui` and, more importantly, set a
    `requestInterceptor`: that attaches the token to every outgoing request
    directly, independent of Swagger's auth store, so "Try it out" works even if
    `preauthorizeApiKey` is unavailable in a given Swagger build. The
    preauthorize calls remain, purely so the padlock icons read as locked.
    """
    if not (settings.docs_autofill_token and _is_loopback(request)):
        return get_swagger_ui_html(
            openapi_url=app.openapi_url or "/openapi.json",
            title=f"{app.title} - Swagger UI",
        )

    token = _js_literal(settings.api_token)
    openapi_url = _js_literal(app.openapi_url or "/openapi.json")
    title = f"{app.title} - Swagger UI"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<link rel="stylesheet" href="{SWAGGER_CSS}">
<style>
  body {{ margin: 0; background: #fafafa; }}
  #api-banner {{
    font: 13px/1.5 system-ui, "Segoe UI", sans-serif;
    padding: 8px 14px; text-align: center; color: #fff; background: #15803d;
  }}
</style>
</head>
<body>
<div id="api-banner">&#10003; Authorized automatically (local request) &mdash;
  &ldquo;Try it out&rdquo; works on every endpoint.</div>
<div id="swagger-ui"></div>
<script src="{SWAGGER_JS}"></script>
<script>
(function () {{
  var TOKEN = {token};

  window.ui = SwaggerUIBundle({{
    url: {openapi_url},
    dom_id: "#swagger-ui",
    layout: "BaseLayout",
    deepLinking: true,
    showExtensions: true,
    showCommonExtensions: true,
    presets: [SwaggerUIBundle.presets.apis, SwaggerUIBundle.SwaggerUIStandalonePreset],

    // Primary mechanism: stamp the token on every request. Does not rely on
    // Swagger's auth store, so it cannot silently stop working.
    requestInterceptor: function (req) {{
      req.headers = req.headers || {{}};
      if (!req.headers["Authorization"] && !req.headers["X-API-Token"]) {{
        req.headers["X-API-Token"] = TOKEN;
      }}
      return req;
    }},

    // Cosmetic only: make the padlocks show as locked.
    onComplete: function () {{
      try {{
        window.ui.preauthorizeApiKey("ApiTokenHeader", TOKEN);
        window.ui.preauthorizeApiKey("BearerToken", TOKEN);
      }} catch (e) {{
        // Padlocks stay open; requests are still authorized by the interceptor.
        var b = document.getElementById("api-banner");
        if (b) {{
          b.style.background = "#0e7490";
          b.innerHTML = "&#10003; Requests are authorized automatically (local request). " +
            "Padlock icons may still look open &mdash; that is cosmetic.";
        }}
      }}
    }}
  }});
}})();
</script>
</body>
</html>"""
    return HTMLResponse(html)


# ---------------------------------------------------------------------- health
@app.get("/health", tags=["meta"])
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "broker_connected": broker.connected,
        "ssh_sessions": sum(1 for s in pool.statuses().values() if s == "on"),
        "active_runs": len(orchestrator.active_runs()),
        "ws_subscribers": bus.subscriber_count,
    }


@app.get("/settings", tags=["meta"], dependencies=[Depends(require_token)])
async def public_settings() -> dict[str, Any]:
    """Non-secret server config the UI may want to display."""
    return {
        "max_message_mb": settings.max_message_mb,
        "remote_root": settings.remote_root,
        "shards_dir": str(settings.shards_path),
        "fanout_concurrency": settings.fanout_concurrency,
        "allow_unsafe_commands": settings.allow_unsafe_commands,
        "metrics_broadcast_hz": settings.metrics_broadcast_hz,
        "metrics_window": settings.metrics_window,
    }


# ------------------------------------------------------------------ websocket
#: Idle ping period. Keeps proxies from reaping the socket and lets us notice a
#: half-open connection.
WS_PING_SECONDS = 20.0


@app.websocket("/ws/stream")
async def ws_stream(websocket: WebSocket) -> None:
    """Push `ssh_status`, `exec_line`, `metrics`, and `event` frames (§7).

    Auth: `?token=<API_TOKEN>` (browsers can't set headers on a WS handshake).
    """
    await websocket.accept()
    if not await authorize_websocket(websocket, websocket.query_params.get("token")):
        return

    async with bus.subscribe() as queue:
        await websocket.send_json(bus.snapshot())

        # Drain any client->server chatter so a `ping` from the UI doesn't pile
        # up in the receive buffer, and so we notice a disconnect promptly.
        async def reader() -> None:
            try:
                while True:
                    await websocket.receive_text()
            except (WebSocketDisconnect, RuntimeError):
                pass

        reader_task = asyncio.create_task(reader())
        try:
            while True:
                if reader_task.done():
                    break
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=WS_PING_SECONDS)
                except asyncio.TimeoutError:
                    await websocket.send_json({"type": "ping"})
                    continue
                await websocket.send_json(message)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reader_task
            with contextlib.suppress(Exception):
                await websocket.close()


# ------------------------------------------------------------- error envelopes
@app.exception_handler(ValueError)
async def value_error_handler(_request: Any, exc: ValueError) -> JSONResponse:
    """Orchestrator preconditions raise ValueError; surface them as 400s rather
    than 500s so the UI can show the message."""
    return JSONResponse(status_code=400, content={"detail": str(exc)})


# ------------------------------------------------------------------------- ui
# Registered last, deliberately: the UI router ends in a `/{asset:path}`
# catch-all, and FastAPI matches in registration order. Mounting it any earlier
# would shadow /docs, /health, and every API route below it.
app.include_router(web.router)

if web.is_built():
    log.info("UI available at / (built from %s)", web.WEB_ROOT)
else:
    log.warning("UI not built -- run `python tools/build_web.py`; / explains how")
