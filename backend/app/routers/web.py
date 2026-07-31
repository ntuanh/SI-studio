"""Serve the tracking UI as a website (guide §8).

`tools/build_web.py` unpacks `split-inference-pipeline.html` into `backend/web/`.
This mounts that directory at the app root so the whole system is one origin:
the page, its REST calls, and `/ws/stream`. Same-origin also means the UI never
needs a CORS exception and never has to be told where the backend is.

The only dynamic piece is `runtime-config.js`, which tells the page its base URL
and -- for local callers only -- the API token.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, RedirectResponse, Response

from ..config import BACKEND_ROOT, settings

log = logging.getLogger(__name__)

router = APIRouter(tags=["ui"], include_in_schema=False)

WEB_ROOT = BACKEND_ROOT / "web"
INDEX = WEB_ROOT / "index.html"

#: Same rule as `/docs` token autofill: a direct connection from this machine,
#: read off the socket rather than a forwardable header.
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"}

NOT_BUILT = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Split Inference Studio</title>
<style>
 body {{ font: 15px/1.6 system-ui, "Segoe UI", sans-serif; background: #0F172A; color: #E2E8F0;
        margin: 0; display: flex; min-height: 100vh; align-items: center; justify-content: center; }}
 main {{ max-width: 620px; padding: 32px; }}
 h1 {{ font-size: 20px; margin: 0 0 12px; }}
 code {{ background: #1E293B; border: 1px solid #334155; border-radius: 6px;
         padding: 2px 7px; font-family: ui-monospace, monospace; font-size: 13px; }}
 pre {{ background: #1E293B; border: 1px solid #334155; border-radius: 9px; padding: 14px;
        overflow-x: auto; }}
 a {{ color: #22D3EE; }}
</style></head>
<body><main>
<h1>The UI has not been built yet</h1>
<p>The website is generated from the bundled UI, which keeps
<code>{source}</code> as the single source of truth. Build it once:</p>
<pre>python tools/build_web.py</pre>
<p>then reload this page. The API itself is already running &mdash;
see <a href="/docs">/docs</a> and <a href="/health">/health</a>.</p>
</main></body></html>
"""


def is_built() -> bool:
    return INDEX.is_file()


def _is_loopback(request: Request) -> bool:
    client = request.client
    return bool(client and client.host in LOOPBACK_HOSTS)


def _safe_asset(rel: str) -> Path | None:
    """Resolve `rel` under WEB_ROOT, or None if it escapes or does not exist."""
    try:
        target = (WEB_ROOT / rel).resolve()
        target.relative_to(WEB_ROOT.resolve())
    except (ValueError, OSError):
        return None
    return target if target.is_file() else None


@router.get("/")
async def index() -> Response:
    if not is_built():
        return Response(
            NOT_BUILT.format(source="split-inference-pipeline.html"),
            media_type="text/html",
            status_code=503,
        )
    # no-store: the page is rebuilt in place by tools/build_web.py, and a
    # cached copy pointing at stale asset names is a confusing failure.
    return FileResponse(INDEX, headers={"Cache-Control": "no-store"})


@router.get("/runtime-config.js")
async def runtime_config(request: Request) -> Response:
    """Hand the page its origin, and the API token only when it is local.

    Binding to 0.0.0.0 therefore does not give the token to every visitor: a
    remote browser gets an empty one and the header chip opens a dialog to paste
    it. `WEB_AUTOFILL_TOKEN=false` turns the local shortcut off too.
    """
    local = settings.web_autofill_token and _is_loopback(request)
    body = "window.__SPLIT_INFERENCE_BOOTSTRAP = {};\n".format(
        json.dumps(
            {
                # Empty string -> the client falls back to window.location.origin,
                # which is what we want behind a proxy or a different hostname.
                "baseUrl": "",
                "token": settings.api_token if local else "",
                "served": True,
                "local": local,
            }
        )
    )
    # Never cached: the response varies by client address, which no cache key
    # covers, so a shared cache could hand a remote browser the local answer.
    return Response(body, media_type="text/javascript", headers={"Cache-Control": "no-store"})


@router.get("/{asset:path}")
async def asset(asset: str) -> Response:
    """Static files from the build.

    Declared last so every real API route wins; unknown paths 404 as files
    rather than being rewritten to index.html, because this is a single page
    with no client-side router to hand them to.
    """
    target = _safe_asset(asset) if asset else None
    if target is None:
        if not is_built():
            return RedirectResponse("/")
        return Response("not found", status_code=404, media_type="text/plain")

    # Images are named after their bundle uuid, so their content cannot change
    # under a fixed URL -- cache them hard. `vendor/` names are stable across
    # rebuilds *and* their contents can change (a UI re-export can ship a
    # different React or dc-runtime), so they must revalidate; FileResponse
    # sends an ETag, making that a cheap 304 rather than a re-download.
    cache = "public, max-age=86400" if asset.startswith("assets/") else "no-cache"
    return FileResponse(target, headers={"Cache-Control": cache})
