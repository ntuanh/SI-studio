"""The website: the build that produces it, and the routes that serve it.

The point of these is that the site is *generated*, so the two ways it can
silently rot are (a) the UI bundle changing shape under `tools/build_web.py`,
and (b) the catch-all asset route shadowing the API.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT / "tools"))

import build_web  # noqa: E402


# --------------------------------------------------------------------- build
@pytest.fixture(scope="module")
def built(tmp_path_factory) -> Path:
    """Run the real build against the real bundle, into a throwaway directory."""
    if not build_web.DEFAULT_SOURCE.exists():
        pytest.skip(f"UI bundle not present: {build_web.DEFAULT_SOURCE}")
    out = tmp_path_factory.mktemp("web")
    build_web.build(build_web.DEFAULT_SOURCE, out)
    return out


def test_build_emits_a_self_contained_site(built: Path) -> None:
    for rel in ("index.html", "backend-client.js", "runtime-config.js",
                "vendor/react.js", "vendor/react-dom.js", "vendor/dc-runtime.js"):
        assert (built / rel).is_file(), f"missing {rel}"

    html = (built / "index.html").read_text(encoding="utf-8")
    # Nothing may be fetched from a CDN: the control plane can run air-gapped.
    assert "https://unpkg.com" not in html
    assert "cdn.jsdelivr.net" not in html
    assert '<script src="vendor/react.js">' in html


def test_build_applies_both_patches(built: Path) -> None:
    html = (built / "index.html").read_text(encoding="utf-8")
    # The header group...
    assert "onSiToggleMode" in html
    assert "siChip.onClick" in html
    # ...and the bridge, inside the logic script rather than after it.
    assert "appended by tools/build_web.py" in html
    logic_start = html.index(build_web.SCRIPT_OPEN)
    assert html.index("Object.assign(P, {") > logic_start
    assert html.index("Object.assign(P, {") < html.index("</script>", logic_start)


def test_build_rewrites_every_bundle_asset(built: Path) -> None:
    """A stray uuid means an asset would 404 at runtime."""
    html = (built / "index.html").read_text(encoding="utf-8")
    manifest = json.loads(
        build_web._script_body(
            build_web.DEFAULT_SOURCE.read_text(encoding="utf-8"), "__bundler/manifest"
        )
    )
    for uuid in manifest:
        for marker in (f'src="{uuid}"', f'href="{uuid}"'):
            assert marker not in html, f"unrewritten asset reference {marker}"


def test_rebuilding_is_idempotent_and_prunes_stale_files(built: Path) -> None:
    """Rebuilding in place (which happens while uvicorn is serving the
    directory) must produce the same bytes and clean up what it dropped."""
    original = json.loads((built / "BUILD.json").read_text(encoding="utf-8"))["files"]

    # Pretend the previous build wrote a file this one no longer emits.
    stale = built / "vendor" / "left-over.js"
    stale.write_text("// from an older build", encoding="utf-8")
    record = json.loads((built / "BUILD.json").read_text(encoding="utf-8"))
    record["files"]["vendor/left-over.js"] = "0" * 64
    (built / "BUILD.json").write_text(json.dumps(record), encoding="utf-8")

    rebuilt = build_web.build(build_web.DEFAULT_SOURCE, built)

    assert not stale.exists(), "a file the previous build wrote was not pruned"
    assert rebuilt == original, "rebuilding the same source changed the output"


def test_build_fails_loudly_when_an_anchor_moves() -> None:
    """A UI re-export that renames the theme button must not build a site with
    the toggle silently missing."""
    src = build_web.DEFAULT_SOURCE.read_text(encoding="utf-8")
    template = json.loads(build_web._script_body(src, "__bundler/template"))
    broken = template.replace(build_web.HEADER_ANCHOR, "<button data-renamed")

    # Leave the assets un-rewritten and point `runtime` at the uuid the raw
    # template already carries, so this exercises the header check alone.
    runtime_uuid = re.search(r'<script src="([0-9a-f-]{36})">', template).group(1)
    with pytest.raises(build_web.BuildError, match="header anchor"):
        build_web._patch_template(
            broken,
            assets={},
            vendor={"react": "r.js", "react_dom": "rd.js", "runtime": runtime_uuid},
            live_patch="",
            favicon=None,
        )


# -------------------------------------------------------------------- routes
def test_index_is_served(client) -> None:
    r = client.get("/")
    if r.status_code == 503:
        pytest.skip("site not built in this checkout (run tools/build_web.py)")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Split Inference Studio" in r.text
    assert r.headers["cache-control"] == "no-store"


def test_runtime_config_withholds_the_token_from_remote_callers(client) -> None:
    """TestClient is not loopback, so this is the remote case."""
    r = client.get("/runtime-config.js")
    assert r.status_code == 200
    assert "text/javascript" in r.headers["content-type"]
    body = json.loads(r.text.split("=", 1)[1].strip().rstrip(";\n"))
    assert body["token"] == ""
    assert body["local"] is False
    assert r.headers["cache-control"] == "no-store"


def test_runtime_config_hands_the_token_to_a_local_browser(client, monkeypatch) -> None:
    from app.routers import web

    monkeypatch.setattr(web, "_is_loopback", lambda request: True)
    body = json.loads(client.get("/runtime-config.js").text.split("=", 1)[1].strip().rstrip(";\n"))
    assert body["token"] == "test-token"
    assert body["local"] is True


def test_web_autofill_can_be_switched_off(client, monkeypatch) -> None:
    from app.routers import web

    monkeypatch.setattr(web, "_is_loopback", lambda request: True)
    monkeypatch.setattr(web.settings, "web_autofill_token", False)
    body = json.loads(client.get("/runtime-config.js").text.split("=", 1)[1].strip().rstrip(";\n"))
    assert body["token"] == ""


def test_asset_route_does_not_shadow_the_api(client, auth) -> None:
    """The catch-all is registered last; every real route must still win."""
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/devices").status_code == 401          # auth, not 404
    assert client.get("/devices", headers=auth).status_code == 200
    assert client.get("/metrics/latest", headers=auth).status_code == 200


def test_assets_cannot_escape_the_web_root(client) -> None:
    for path in ("../.env", "..%2f.env", "vendor/../../.env", "/etc/passwd"):
        r = client.get(f"/{path}")
        assert r.status_code in (404, 400), f"{path} -> {r.status_code}"
        assert "API_TOKEN" not in r.text


def test_the_site_needs_no_token(client) -> None:
    """The page itself is public; its API calls are what carry the token.

    Serving index.html behind auth would be unusable -- a browser has nowhere
    to put the header on a top-level navigation.
    """
    for path in ("/", "/runtime-config.js", "/backend-client.js"):
        assert client.get(path).status_code in (200, 503)
