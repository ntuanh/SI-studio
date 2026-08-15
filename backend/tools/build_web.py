#!/usr/bin/env python3
"""Turn the bundled UI into a website this backend can serve.

`split-inference-pipeline.html` is a self-extracting bundle: the real page is a
JSON string inside `<script type="__bundler/template">`, and its assets (the DC
runtime, React, the logo) are base64 blobs in `<script type="__bundler/manifest">`.
Opened from disk it unpacks itself into a blob iframe -- fine for a demo, wrong
for a tracked deployment: `file://` origins, no shared state, and the Control
tab's handlers are `setTimeout` mocks.

So this unpacks the bundle once, at build time, into `backend/web/`:

    web/index.html          the inner page, assets rewritten to local paths
    web/vendor/*.js         dc-runtime, react, react-dom (no CDN at runtime)
    web/assets/*            images
    web/backend-client.js   the transport layer (copied from ui/)

and applies two patches on the way through:

  1. a header group -- Live/Simulate toggle, connection chip, Deploy button;
  2. `ui/live-patch.js` appended to the page's `<script type="text/x-dc">`,
     which overrides the mocked methods on `Component.prototype`.

The source HTML is never modified. Re-export the UI, re-run this, and the
wiring is re-applied from source -- that is the reason the patch is appended
rather than merged into the original method bodies.

    python tools/build_web.py                 # build into backend/web
    python tools/build_web.py --check         # verify an existing build is current
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import gzip
import hashlib
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = BACKEND_ROOT.parent / "split-inference-pipeline.html"
DEFAULT_OUT = BACKEND_ROOT / "web"
UI_DIR = BACKEND_ROOT / "ui"

MIME_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "text/javascript": ".js",
    "application/javascript": ".js",
    "text/css": ".css",
}


class BuildError(RuntimeError):
    """A patch anchor moved, or the bundle isn't shaped the way we expect."""


# --------------------------------------------------------------- bundle parsing
def _script_body(html: str, script_type: str) -> str:
    """Contents of `<script type="...">`, which the bundler never nests."""
    open_tag = f'<script type="{script_type}">'
    start = html.find(open_tag)
    if start < 0:
        raise BuildError(f"no <script type={script_type!r}> in the bundle")
    start += len(open_tag)
    end = html.find("</script>", start)
    if end < 0:
        raise BuildError(f"unterminated <script type={script_type!r}>")
    return html[start:end]


def _decode_asset(entry: dict) -> bytes:
    raw = base64.b64decode(entry["data"])
    return gzip.decompress(raw) if entry.get("compressed") else raw


def _previous_files(out: Path) -> set[str]:
    """What the last build wrote, so this one can prune what it no longer needs."""
    record = out / "BUILD.json"
    if not record.is_file():
        return set()
    try:
        return set(json.loads(record.read_text(encoding="utf-8")).get("files", {}))
    except (OSError, ValueError):
        return set()


def _prune(out: Path, previous: set[str], written: dict[str, str]) -> list[str]:
    """Delete files the previous build wrote that this one did not."""
    removed = []
    for rel in sorted(previous - set(written)):
        path = out / rel
        try:
            path.unlink()
            removed.append(rel)
        except OSError:
            continue
    for directory in ("vendor", "assets"):
        with contextlib.suppress(OSError):
            (out / directory).rmdir()  # only succeeds when empty
    return removed


# ------------------------------------------------------------------- patch 1/2
#: Inserted immediately before the theme toggle, so it lands inside the header's
#: existing flex row and inherits its wrapping.
HEADER_ANCHOR = '<button sc-camel-on-click="{{ onToggleTheme }}"'

HEADER_MARKUP = """<div sc-camel-on-click="{{ siChip.onClick }}" title="Backend URL and API token" style="{{ siChip.style }}">
        <span style="{{ siChip.dotStyle }}"></span>{{ siChip.label }}
      </div>
      <button sc-camel-on-click="{{ onSiToggleMode }}" title="Where the inference numbers come from: Live = measured by the backend, Simulate = in-browser math. The Control tab always talks to real machines." style="{{ siModeStyle }}">{{ siModeLabel }}</button>
      <button sc-camel-on-click="{{ onSiMeasureAll }}" title="{{ siMeasureAllTitle }}" style="{{ siMeasureAllStyle }}">{{ siMeasureAllLabel }}</button>
      <button sc-camel-on-click="{{ onSiDeploy }}" title="Push shards + agents to every cluster" style="{{ siDeployStyle }}">&#8679; Deploy</button>
      """


# ------------------------------------------------------------------- patch 2/3
#: The server card ships as four fields that read as generic "server" config.
#: The host really wears three hats (SSH, AMQP, control API) with two different
#: logins, so the four existing inputs are relabelled as the **SSH** login --
#: which is what an operator types into a box labelled user/password next to an
#: IP -- and the AMQP login moves to its own row below.
CARD_LABELS = [
    ('>Port</span><input type="number" value="{{ ssh.server.port }}',
     '>SSH port</span><input type="number" value="{{ ssh.server.port }}'),
    ('>Username</span><input value="{{ ssh.server.user }}" sc-camel-on-change="{{ ssh.server.onUser }}" placeholder="admin"',
     '>SSH user</span><input value="{{ ssh.server.user }}" sc-camel-on-change="{{ ssh.server.onUser }}" placeholder="dai"'),
    ('>Password</span><input type="password" value="{{ ssh.server.password }}',
     '>SSH password</span><input type="password" value="{{ ssh.server.password }}'),
    ('<div style="font-size:13px; font-weight:700;">Broker / backend server</div>'
     '<div style="font-size:10px; color:var(--muted);">AMQP + control API host</div>',
     '<div sc-camel-on-click="{{ ssh.server.onToggleCard }}" style="font-size:13px; font-weight:700; cursor:pointer;">Control server</div>'
     '<div style="font-size:10px; color:var(--muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">{{ ssh.server.subtitle }}</div>'),
    # The Targets row hardcodes a "c" before the cluster number, which renders
    # as a bare "c" on the control server -- it belongs to no cluster. Font
    # size is what distinguishes this badge from the identical one in the
    # pipeline stage list, which must keep its literal prefix.
    ('<span style="font-size:9px; color:var(--muted);" class="num">c{{ d.cluster }}</span>',
     '<span style="font-size:9px; color:var(--muted);" class="num">{{ d.clusterLabel }}</span>'),
]

#: The whole Test-connection button, replaced so the primary action reads as
#: "get a shell" and the broker moves below it, collapsed.
#: Collapse the card once it has done its job. Connected, it is a tall block of
#: settings nobody is reading, pushing the Targets list off screen.
SERVER_HEADER = ('<span title="{{ ssh.server.statusLabel }}" style="width:9px; height:9px; '
                 'border-radius:50%; background:{{ ssh.server.dot }};"></span>\n'
                 '              </div>')

SERVER_HEADER_NEW = ('<span title="{{ ssh.server.statusLabel }}" style="width:9px; height:9px; '
                     'border-radius:50%; background:{{ ssh.server.dot }};"></span>\n'
                     '                <button sc-camel-on-click="{{ ssh.server.onToggleCard }}" '
                     'title="{{ ssh.server.cardToggleTitle }}" style="background:none; border:none; '
                     'color:var(--muted); font-size:12px; cursor:pointer; padding:0 2px;">'
                     '{{ ssh.server.cardCaret }}</button>\n'
                     '              </div>\n'
                     '              <sc-if value="{{ ssh.server.expanded }}" hint-placeholder-val="{{ true }}">')

CARD_BUTTON = (
    '<button sc-camel-on-click="{{ ssh.server.onTest }}" style="background:var(--broker); '
    'color:#fff; border:none; border-radius:8px; padding:7px; font-size:11px; '
    'font-weight:700; cursor:pointer;">Test connection</button>'
)

_INPUT = ('background:var(--bg); border:1px solid var(--border); border-radius:6px; '
          'color:var(--ink); font-family:ui-monospace,monospace; font-size:11px; '
          'padding:5px 6px; width:100%; min-width:0;')
#: `min-width:0` on both label and input: a grid item's default `min-width:auto`
#: floors it at the input's intrinsic width (~180px in Chrome), which overflows
#: three columns in a card this narrow and clips the last field.
_LABEL = 'display:flex; flex-direction:column; gap:2px; min-width:0;'
_CAPTION = 'font-size:9px; text-transform:uppercase; color:var(--muted);'

#: Goes *before* the button: the jump toggle and the result line.
CARD_MARKUP = f"""<label sc-camel-on-click="{{{{ ssh.server.onToggleJump }}}}" title="Devices are dialled over a channel on this server's connection (ProxyJump)" style="display:flex; align-items:center; gap:7px; cursor:pointer; font-size:10px; color:var(--muted);">
                <span style="{{{{ ssh.server.jumpBox }}}}">{{{{ ssh.server.jumpCheck }}}}</span>
                <span style="flex:1;">Reach devices through this server</span>
              </label>
              <div style="font-size:10px; font-family:ui-monospace,monospace; color:{{{{ ssh.server.bannerColor }}}}; word-break:break-all;">{{{{ ssh.server.banner }}}}</div>
              """

#: Replaces the button: an SSH-first primary action, with the broker demoted to
#: a collapsed section. RabbitMQ matters only once you are running split
#: inference; it has no business standing between an operator and a shell.
CARD_TAIL = f"""<button sc-camel-on-click="{{{{ ssh.server.onTest }}}}" style="{{{{ ssh.server.testStyle }}}}">{{{{ ssh.server.testLabel }}}}</button>
              <div sc-camel-on-click="{{{{ ssh.server.onToggleBroker }}}}" style="display:flex; align-items:center; gap:6px; cursor:pointer; {_CAPTION} font-weight:700; letter-spacing:.05em; border-top:1px solid var(--border); padding-top:9px;">
                <span>{{{{ ssh.server.brokerCaret }}}}</span><span style="flex:1;">Broker (RabbitMQ) &middot; optional</span>
              </div>
              <sc-if value="{{{{ ssh.server.showBroker }}}}" hint-placeholder-val="{{{{ false }}}}">
                <div style="display:flex; flex-direction:column; gap:7px;">
                  <div style="font-size:10px; color:var(--muted);">Only needed to run split inference, when the devices publish feature maps to a queue.</div>
                  <div style="display:grid; grid-template-columns:2fr 1fr; gap:7px;">
                    <label style="{_LABEL}"><span style="{_CAPTION}">AMQP host</span><input value="{{{{ ssh.server.amqpHost }}}}" sc-camel-on-change="{{{{ ssh.server.onAmqpHost }}}}" placeholder="{{{{ ssh.server.amqpHostHint }}}}" style="{_INPUT}"></label>
                    <label style="{_LABEL}"><span style="{_CAPTION}">AMQP port</span><input type="number" value="{{{{ ssh.server.amqpPort }}}}" sc-camel-on-change="{{{{ ssh.server.onAmqpPort }}}}" style="{_INPUT}" class="num"></label>
                    <label style="{_LABEL}"><span style="{_CAPTION}">AMQP user</span><input value="{{{{ ssh.server.amqpUser }}}}" sc-camel-on-change="{{{{ ssh.server.onAmqpUser }}}}" placeholder="guest" style="{_INPUT}"></label>
                    <label style="{_LABEL}"><span style="{_CAPTION}">AMQP pass</span><input type="password" value="{{{{ ssh.server.amqpPassword }}}}" sc-camel-on-change="{{{{ ssh.server.onAmqpPassword }}}}" placeholder="••••••" style="{_INPUT}"></label>
                  </div>
                  <button sc-camel-on-click="{{{{ ssh.server.onTestAll }}}}" style="background:var(--surface); color:var(--muted); border:1px solid var(--border); border-radius:8px; padding:6px; font-size:10px; font-weight:600; cursor:pointer;">Check broker + control API too</button>
                </div>
              </sc-if>
              </sc-if>"""  # the second closes the whole-card collapse


# ------------------------------------------------------------------ patch 2f/3
#: "select all" moves off the Targets header and onto each stage.
#:
#: One button for the whole list is the wrong grain: a fan-out command is aimed
#: at a stage -- every edge box, or just the stage-2 devices -- so the global
#: version meant ticking all thirteen and unticking ten, or clicking rows one
#: at a time. The header slot it vacates shows the running count instead, which
#: is what the Run and Files cards below actually act on.
TARGETS_SELECT_ALL = (
    '<button sc-camel-on-click="{{ ssh.onSelectAll }}" style="background:none; '
    'border:none; color:var(--edge); font-size:11px; font-weight:600; '
    'cursor:pointer;">{{ ssh.selectAllLabel }}</button>'
)

TARGETS_SELECT_ALL_NEW = (
    '<div style="font-size:11px; color:var(--muted);" class="num">'
    "{{ ssh.selectedCount }} selected</div>"
)

#: The stage's own toggle, right-aligned in the group's caption row. It opts out
#: of the row's uppercase/letter-spacing: "SELECT ALL" shouting next to the
#: stage name reads as a second heading rather than as an action.
GROUP_HEADER = (
    '<div style="display:flex; align-items:center; gap:6px; font-size:11px; '
    'font-weight:700; text-transform:uppercase; letter-spacing:.04em; '
    'color:{{ g.color }}; margin-bottom:6px;">{{ g.name }}</div>'
)

GROUP_HEADER_NEW = (
    '<div style="display:flex; align-items:center; gap:6px; font-size:11px; '
    'font-weight:700; text-transform:uppercase; letter-spacing:.04em; '
    'color:{{ g.color }}; margin-bottom:6px;">'
    '<span style="flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; '
    'white-space:nowrap;">{{ g.name }}</span>'
    '<button sc-camel-on-click="{{ g.onSelectAll }}" title="{{ g.selectAllTitle }}" '
    'style="background:none; border:none; color:{{ g.color }}; font-size:10px; '
    'font-weight:600; text-transform:none; letter-spacing:0; cursor:pointer; '
    'padding:0; flex-shrink:0;">{{ g.selectAllLabel }}</button></div>'
)


# ------------------------------------------------------------------ patch 2b/3
#: A working-directory box above the command line, and an "edit" chip on the
#: preset row. `cd` cannot work as a standalone command -- each command gets a
#: fresh shell -- so the directory has to be part of every request.
CWD_ANCHOR = '<div style="display:flex; gap:8px; align-items:stretch;">\n                  <span style="display:flex; align-items:center; padding:0 10px; background:var(--bg); border:1px solid var(--border); border-right:none; border-radius:9px 0 0 9px; color:var(--data); font-family:ui-monospace,monospace; font-size:13px; font-weight:700;">$</span>'

# ------------------------------------------------------------------ patch 2d/3
#: The SCP card only pushes. Pulling a result file back off a device is the
#: other half of the job and there was no way to do it at all -- so the card
#: gains a browse/pull row underneath.
SCP_TITLE = ('<div style="font-size:13px; font-weight:700; margin-bottom:10px;">'
             "SCP — push file to selected devices</div>")
SCP_TITLE_NEW = ('<div style="font-size:13px; font-weight:700; margin-bottom:10px;">'
                 "Files — push to selected devices, pull from one</div>")

_PULL_INPUT = ('background:var(--bg); border:1px solid var(--border); border-radius:9px; '
               'color:var(--ink); font-family:ui-monospace,monospace; font-size:12px; '
               'padding:9px 10px; outline:none; min-width:0;')

PULL_ANCHOR = '<button sc-camel-on-click="{{ ssh.onScp }}" disabled="{{ ssh.runDisabled }}" style="{{ ssh.scpStyle }}">Send</button>\n                </div>'

PULL_MARKUP = f"""<button sc-camel-on-click="{{{{ ssh.onScp }}}}" disabled="{{{{ ssh.runDisabled }}}}" style="{{{{ ssh.scpStyle }}}}">Send</button>
                </div>
                <div style="display:grid; grid-template-columns:auto 1fr auto auto; gap:8px; align-items:center; margin-top:8px; padding-top:10px; border-top:1px solid var(--border);">
                  <span style="font-size:10px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); font-weight:700;">{{{{ ssh.pullFromLabel }}}}</span>
                  <input value="{{{{ ssh.pullPath }}}}" sc-camel-on-change="{{{{ ssh.onPullPath }}}}" placeholder="path on the device, e.g. ntuanh/app.log" style="{_PULL_INPUT}">
                  <button sc-camel-on-click="{{{{ ssh.onBrowse }}}}" title="List this directory on the device" style="{{{{ ssh.browseStyle }}}}">&#9776; browse</button>
                  <button sc-camel-on-click="{{{{ ssh.onPull }}}}" title="Download this file to your machine" style="{{{{ ssh.pullStyle }}}}">&#8681; Pull</button>
                </div>
                <div style="{{{{ ssh.browseRowStyle }}}}">
                  <sc-for list="{{{{ ssh.browseEntries }}}}" as="f" hint-placeholder-count="4">
                    <div sc-camel-on-click="{{{{ f.onPick }}}}" title="{{{{ f.path }}}}" style="{{{{ f.style }}}}">
                      <span style="flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">{{{{ f.name }}}}</span>
                      <span style="color:var(--muted); font-size:10px;" class="num">{{{{ f.size }}}}</span>
                    </div>
                  </sc-for>
                </div>"""


# ------------------------------------------------------------------ patch 2e/3
#: The other half of "the run finished": point at the directory it wrote and
#: get charts back. Its own section at the foot of the nav, after Control.
#:
#: It began as a card inside the Control tab and outgrew it -- a gallery of
#: charts is a lot of card to keep above a terminal you are trying to work in,
#: and reading results is a separate sitting from driving machines. As a
#: section it needs no collapse (see `viz` in ui/live-patch.js, where the
#: toggle went away with it): the tab *is* the disclosure.
#:
#: Rendered as <img> rather than a client-side plot: the charts are drawn
#: server-side by `app/reports/charts.py` to `guides/visual_guide.md`, and a
#: saved report has to still be readable after the UI is rebuilt.
_VIZ_INPUT = ('background:var(--bg); border:1px solid var(--border); border-radius:9px; '
              'color:var(--ink); font-family:ui-monospace,monospace; font-size:12px; '
              'padding:9px 10px; outline:none; min-width:0;')
_VIZ_CAPTION = ('font-size:10px; text-transform:uppercase; letter-spacing:.04em; '
                'color:var(--muted); font-weight:700;')

#: Anchored on the last rule of the page's own stylesheet.
#:
#: The rest of this file patches markup, but a gallery needs things inline
#: styles cannot express: a responsive grid that reflows with the window, a
#: hover state, and a `:focus` ring on the note boxes. Those go here, scoped
#: under `viz-` so nothing else on the page can pick them up, and they inherit
#: the page's own light/dark custom properties rather than restating colors.
CSS_ANCHOR = "  .num { font-variant-numeric: tabular-nums; }"

VISUAL_CSS = """
  /* ---- left rail: stays put, half width until pointed at ----
     The page wrapper is `min-height:100vh`, so it grows past the viewport and
     the *document* scrolls -- `<main>`'s own `overflow:auto` never engages. The
     rail therefore has to stick rather than rely on being in a scroll box, and
     `top` has to clear the sticky header.

     The expansion overlays `<main>` instead of widening the flex row: growing
     the row would re-lay-out the chart gallery every time the pointer crossed
     the rail. So `.si-rail` is a fixed-width slot in the flow and
     `.si-rail-inner` is the panel that grows over the top of it. */
  :root {
    --si-head: 61px;      /* the sticky header's height */
    --si-rail: 106px;     /* collapsed: half of the full width */
    --si-rail-open: 212px;
  }
  .si-rail {
    position:sticky; top:var(--si-head); align-self:flex-start;
    flex-shrink:0; width:var(--si-rail); height:calc(100vh - var(--si-head));
  }
  .si-rail-inner {
    position:absolute; top:0; bottom:0; left:0; width:var(--si-rail);
    background:var(--raised); border-right:1px solid var(--border);
    padding:14px 12px; display:flex; flex-direction:column; gap:4px;
    overflow-x:hidden; overflow-y:auto;
    transition:width .16s ease, box-shadow .16s ease;
  }
  /* `focus-within` as well as hover, so the rail is reachable by keyboard on a
     machine with no pointer at all. */
  .si-rail:hover .si-rail-inner,
  .si-rail:focus-within .si-rail-inner {
    width:var(--si-rail-open); z-index:15;
    box-shadow:6px 0 22px rgba(0,0,0,.11);
  }
  .si-rail-label { white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  /* Collapsed, the label gets 28px of the button's 81px and "Simulation" wants
     61px, so every item truncates to two letters -- `St…`, `Pi…`, `Co…` -- which
     names nothing. Dropping the badge and tightening the padding frees 56px,
     and at 11.5px the longest label fits: the rail stays fully readable at half
     width instead of becoming a row of stubs. Hover puts the badges and the
     roomier spacing back.

     `!important` because the button's padding and gap arrive as inline styles
     from the bundle's own renderVals, which a class cannot otherwise outrank. */
  .si-rail-inner button {
    padding-left:5px !important; padding-right:5px !important; gap:6px !important;
  }
  .si-rail-inner .si-rail-label { font-size:11.5px; }
  .si-rail-inner button > .num { display:none; }
  .si-rail:hover .si-rail-inner button,
  .si-rail:focus-within .si-rail-inner button {
    padding-left:10px !important; padding-right:10px !important; gap:9px !important;
  }
  .si-rail:hover .si-rail-inner .si-rail-label,
  .si-rail:focus-within .si-rail-inner .si-rail-label { font-size:13px; }
  .si-rail:hover .si-rail-inner button > .num,
  .si-rail:focus-within .si-rail-inner button > .num { display:inline; }
  /* The legend needs the full width to read, so it fades in with the rail. It
     keeps its space while hidden -- collapsing it too would move the items
     under the pointer as the rail opened. */
  .si-rail-legend { opacity:0; pointer-events:none; transition:opacity .14s ease; }
  .si-rail:hover .si-rail-legend,
  .si-rail:focus-within .si-rail-legend { opacity:1; pointer-events:auto; }
  @media (max-width:760px) {
    /* A touch screen has no hover to expand with, so nothing collapses. */
    .si-rail, .si-rail-inner { width:var(--si-rail-open); }
    .si-rail-legend { opacity:1; pointer-events:auto; }
    .si-rail-inner button {
      padding-left:10px !important; padding-right:10px !important; gap:9px !important;
    }
    .si-rail-inner .si-rail-label { font-size:13px; }
    .si-rail-inner button > .num { display:inline; }
  }

  /* ---- Visual tab ---- */
  .viz-col { display:flex; flex-direction:column; gap:14px; max-width:1280px; }
  .viz-card {
    background:var(--raised); border:1px solid var(--border); border-radius:14px;
    padding:14px 15px;
  }
  .viz-row { display:grid; grid-template-columns:auto 1fr auto; gap:9px; align-items:center; }
  /* The window row's controls travel together and wrap as a unit: "5 to 90 %
     of the run" split across two lines reads as two settings. */
  .viz-window { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
  .viz-window-to { font-size:11px; color:var(--muted); white-space:nowrap; }
  .viz-tiles {
    display:grid; gap:10px;
    grid-template-columns:repeat(auto-fit, minmax(155px, 1fr));
  }
  .viz-tile {
    background:var(--raised); border:1px solid var(--border); border-radius:12px;
    padding:11px 13px; display:flex; flex-direction:column; gap:3px;
    position:relative; overflow:hidden;
  }
  /* The accent is a 3px rail, not a tinted fill: the number is the mark, and a
     colored card would compete with it. The rail element is supplied by the
     renderer because its color carries the tile's own status. */
  .viz-tile-label {
    font-size:9.5px; text-transform:uppercase; letter-spacing:.05em;
    color:var(--muted); font-weight:700;
  }
  .viz-tile-value { font-size:26px; line-height:1.05; font-weight:700; color:var(--ink); }
  .viz-tile-unit { font-size:12px; font-weight:600; color:var(--muted); margin-left:5px; }
  .viz-tile-delta { font-size:10px; color:var(--muted); }
  .viz-gallery {
    display:grid; gap:12px;
    grid-template-columns:repeat(auto-fill, minmax(430px, 1fr));
  }
  .viz-chart {
    background:var(--raised); border:1px solid var(--border); border-radius:14px;
    padding:12px 13px 13px; display:flex; flex-direction:column; min-width:0;
    transition:border-color .16s ease, box-shadow .16s ease;
  }
  .viz-chart:hover { border-color:var(--muted); box-shadow:0 4px 16px rgba(0,0,0,.07); }
  .viz-chart img {
    width:100%; display:block; border-radius:9px; background:#fcfcfb;
    border:1px solid var(--border); cursor:zoom-in;
  }
  .viz-index {
    font-size:10px; font-weight:700; color:var(--muted); font-variant-numeric:tabular-nums;
    background:var(--bg); border:1px solid var(--border); border-radius:6px;
    padding:1px 6px; flex-shrink:0;
  }
  .viz-chip {
    font-size:9px; font-weight:700; text-transform:uppercase; letter-spacing:.05em;
    border-radius:999px; padding:2px 8px; flex-shrink:0;
  }
  .viz-note {
    background:var(--bg); border:1px solid var(--border); border-radius:9px;
    color:var(--ink); font-size:12px; padding:8px 10px; outline:none;
    width:100%; margin-top:9px; transition:border-color .16s ease;
  }
  .viz-note:focus { border-color:var(--data); }
  /* Quiet, but never invisible: it has to be findable without hovering every
     card to discover that charts are configurable at all. */
  .viz-gear {
    background:none; border:1px solid var(--border); border-radius:7px;
    color:var(--muted); font-size:13px; line-height:1; padding:3px 7px;
    cursor:pointer; opacity:.75; transition:opacity .16s ease, border-color .16s ease;
  }
  .viz-chart:hover .viz-gear { opacity:1; }
  .viz-gear:hover { border-color:var(--data); color:var(--data); }
  .viz-config {
    background:var(--bg); border:1px solid var(--border); border-radius:11px;
    padding:11px 12px; margin-bottom:10px;
  }
  .viz-config input:focus { border-color:var(--data); }
  /* A history entry is one pill with two targets: the label opens the report,
     the ✕ deletes it. The delete stays faint until the pill is hovered, so the
     row reads as a list of runs rather than a row of delete buttons. */
  .viz-run { display:inline-flex; align-items:stretch; overflow:hidden; }
  .viz-run-open {
    background:none; border:none; color:inherit; font:inherit; cursor:pointer;
    padding:6px 4px 6px 11px; white-space:nowrap;
  }
  .viz-kill {
    background:none; border:none; color:inherit; font:inherit; cursor:pointer;
    padding:6px 9px 6px 5px; opacity:.35;
    transition:opacity .16s ease, color .16s ease;
  }
  .viz-run:hover .viz-kill { opacity:.85; }
  .viz-kill:hover { opacity:1; color:var(--alert); }
  .viz-empty {
    border:1px dashed var(--border); border-radius:14px; padding:34px 20px;
    text-align:center; color:var(--muted); font-size:12.5px; line-height:1.6;
  }

  /* ---- compare mode ----
     Every block in here is laid out on the grid template the renderer builds
     once (`cmpGrid`): a label column, then one column per pinned report. The
     column headers, the numbers table and each chart row all use it, which is
     what makes a column mean the same report the whole way down. It is set
     inline rather than in a class because only the renderer knows whether two
     or three reports are pinned.

     Columns are `minmax(0, 1fr)` -- they squeeze rather than overflow. A
     sideways scrollbar under a comparison is the one thing that could put the
     columns out of alignment on screen, which is the whole point of the mode. */
  .viz-cmp { display:flex; flex-direction:column; gap:11px; }
  .viz-cmp-row {
    background:var(--bg); border:1px solid var(--border); border-radius:12px;
    padding:10px 11px;
  }
  .viz-cmp-row + .viz-cmp-row { margin-top:10px; }
  .viz-cmp-id {
    display:flex; flex-direction:column; align-items:flex-start; gap:5px;
    min-width:0; padding-top:2px;
  }
  .viz-cmp-title {
    font-size:12px; font-weight:700; color:var(--ink); line-height:1.3;
    overflow-wrap:anywhere;
  }
  .viz-cmp-focus {
    background:none; border:1px solid var(--border); border-radius:7px;
    color:var(--muted); font-size:9.5px; font-weight:700; padding:2px 7px;
    cursor:pointer; white-space:nowrap;
  }
  .viz-cmp-focus:hover { border-color:var(--data); color:var(--data); }
  .viz-cmp-cell { display:flex; flex-direction:column; gap:5px; min-width:0; }
  /* Fitted into a box, not scaled to the column.
     The renderer gives every cell in a row the same `aspect-ratio` -- the
     widest figure in that row -- and `object-fit:contain` draws each PNG into
     it: the widest fills the box, the rest are letterboxed into it at the same
     height. That is what puts two runs of the same chart at one scale even
     though the guide drew them at different pixel widths.

     `object-fit` is doing the real work in both directions: the box's shape is
     fixed by the aspect ratio and its height by the cap, so without it an image
     that matched neither would simply be stretched to the box. The PNGs are
     drawn on white, so the letterboxing is invisible.

     The cap keeps a stack of rows the same height to scan past, which is most
     of what makes ten charts surfable; `--viz-cmp-img` comes off the row, where
     the renderer raises it for the one chart that has the page to itself. */
  .viz-cmp-cell img {
    width:100%; display:block; border-radius:8px; background:#fcfcfb;
    border:1px solid var(--border); cursor:zoom-in;
    max-height:var(--viz-cmp-img, 330px); object-fit:contain;
  }
  .viz-cmp-tag {
    display:flex; align-items:center; gap:6px; min-width:0;
    font-size:9.5px; font-weight:700; text-transform:uppercase; letter-spacing:.04em;
  }
  .viz-cmp-tag > span:last-child {
    overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
  }
  /* Stated, not blank: a report that never drew this chart is a fact about the
     run, and an empty cell would read as a broken image. */
  .viz-cmp-miss {
    border:1px dashed var(--border); border-radius:8px; padding:22px 10px;
    text-align:center; color:var(--muted); font-size:10.5px;
  }
  .viz-cmp-note { font-size:10.5px; color:var(--muted); line-height:1.45; }
  .viz-cmp-empty {
    border:1px dashed var(--border); border-radius:12px; padding:22px 18px;
    text-align:center; color:var(--muted); font-size:12px; line-height:1.6;
  }
  /* Padded by exactly the chart row's border + padding, so the column headers,
     the numbers table and the charts all start their first column on the same
     x. Without this the rows are inset by their own card and the columns line
     up with each other but not with the names at the top of them. */
  .viz-cmp-heads, .viz-cmp-tiles { padding-left:12px; padding-right:12px; }
  .viz-cmp-tiles { display:flex; flex-direction:column; }
  .viz-cmp-tile { padding:6px 0; border-top:1px solid var(--border); }
  .viz-cmp-tile:first-child { border-top:none; }
  .viz-cmp-tile-label {
    font-size:10px; text-transform:uppercase; letter-spacing:.04em;
    color:var(--muted); font-weight:700; align-self:center;
  }
  .viz-cmp-tile-value { font-size:15px; font-weight:700; color:var(--ink); }
  .viz-cmp-tile-delta { font-size:10px; color:var(--muted); margin-left:7px; }
  .viz-cmp-surf {
    display:flex; align-items:center; gap:7px; flex-wrap:wrap;
    border-top:1px solid var(--border); padding-top:10px;
  }
  /* The chip row is the map of the whole comparison, so it stays one line high
     and scrolls -- ten charts wrapped over four rows push the charts
     themselves off the screen. */
  .viz-cmp-chips {
    display:flex; gap:6px; align-items:center; flex:1; min-width:0;
    overflow-x:auto; padding-bottom:3px;
  }
  @media (max-width:760px) {
    .viz-row { grid-template-columns:1fr; }
    .viz-gallery { grid-template-columns:1fr; }
  }

  /* ---- pointer feedback ----
     Two halves of one thing: a soft light that trails the cursor, so the eye
     can find it again across a dashboard this wide, and a blink on whatever
     clickable thing it lands on.

     The light is moved by `installPointerFx` in ui/live-patch.js writing a
     transform; the easing lives here, in two different transition durations --
     the ring tracks the pointer almost exactly, the glow lags behind it, and
     the gap between them is what reads as motion. `--si-fx` is set on <body>
     by the same code and holds the hovered control's own accent, so the light
     takes the colour of what it is over (green over Run, red over reboot).

     The blink animates `outline`, deliberately, not `box-shadow`: the rail's
     active item and the auto-balance buttons carry their marker as an inline
     `box-shadow: inset ...`, and a CSS animation outranks an inline style --
     animating that property would erase the marker for as long as the pointer
     sat on it. Outline is also free of layout: nothing shifts as it pulses.

     `currentColor` means each control blinks in its own colour rather than in
     one house tint; the flat rgba before it is the fallback for a browser
     without `color-mix`, and reads on both themes. */
  .si-cursor-glow, .si-cursor-dot {
    position:fixed; left:0; top:0; z-index:2147483000; pointer-events:none;
    border-radius:50%; opacity:0;
  }
  .si-cursor-glow {
    width:260px; height:260px; margin:-130px 0 0 -130px;
    background:radial-gradient(circle closest-side, rgba(100,116,139,.16), transparent 70%);
    background:radial-gradient(circle closest-side,
      color-mix(in srgb, var(--si-fx, var(--edge)) 20%, transparent), transparent 70%);
    transition:transform .34s cubic-bezier(.22,.61,.36,1), opacity .28s ease;
  }
  /* The pale halo is what keeps the ring visible on a filled button whose
     background *is* the accent it has just borrowed -- a green ring on the
     green Run button would vanish at the moment it arrived. It does nothing on
     a light page, where the accent is dark and reads on its own. */
  .si-cursor-dot {
    width:17px; height:17px; margin:-8.5px 0 0 -8.5px;
    border:1.5px solid var(--si-fx, var(--edge));
    box-shadow:0 0 0 1.5px rgba(255,255,255,.5), 0 0 5px rgba(15,23,42,.4);
    transition:transform .07s linear, opacity .2s ease, border-width .15s ease,
               width .15s ease, height .15s ease, margin .15s ease;
  }
  /* Off until the pointer has actually moved: a ring parked in the top-left
     corner of a freshly loaded page is a rendering bug, not an effect. */
  .si-fx-on .si-cursor-glow { opacity:1; }
  .si-fx-on .si-cursor-dot { opacity:.7; }
  /* Over something clickable the ring opens up, which is the same signal the
     blink gives, aimed at the eye that is following the cursor. Thinner and
     not quite opaque as it grows: at this size it lands on top of the label it
     is pointing at, and a hard 1.5px circle through small type is hard to read
     past. */
  .si-fx-on.si-fx-hot .si-cursor-dot {
    width:34px; height:34px; margin:-17px 0 0 -17px; opacity:.8; border-width:1px;
  }
  .si-fx-on.si-fx-tap .si-cursor-dot {
    width:13px; height:13px; margin:-6.5px 0 0 -6.5px; opacity:1;
  }
  @keyframes siBlink {
    0%, 100% { outline-color:transparent; outline-offset:1px; }
    55% {
      outline-color:rgba(100,116,139,.5);
      outline-color:color-mix(in srgb, currentColor 50%, transparent);
      outline-offset:3px;
    }
  }
  /* Hover-capable pointers only: on a touch screen "hover" is a tap that has
     already happened, so the blink would fire after the fact. */
  @media (hover:hover) and (pointer:fine) {
    button:hover:enabled {
      outline:2px solid transparent;
      animation:siBlink 1.05s ease-in-out infinite;
    }
  }
  /* Both effects are motion for its own sake -- the first thing to go when the
     operator has asked the system for less of it. */
  @media (prefers-reduced-motion: reduce) {
    .si-cursor-glow, .si-cursor-dot { display:none; }
    button:hover:enabled { animation:none; }
  }
"""

VISUAL_SECTION = """
        <!-- ============ VISUAL ============ -->
        <sc-if value="{{ showVisual }}" hint-placeholder-val="{{ false }}">
          <div style="display:flex; align-items:flex-end; justify-content:space-between; gap:16px; margin-bottom:16px; flex-wrap:wrap;">
            <div>
              <h1 style="margin:0; font-size:20px; font-weight:800;">Visual</h1>
              <p style="margin:4px 0 0; font-size:13px; color:var(--muted); max-width:620px;">Chart a finished run. Point at the result directory it wrote on the device &mdash; the charts are drawn on the backend to the visual guide and saved with your notes, so a report stays readable long after the run.</p>
            </div>
            <div style="{{ viz.headlineStyle }}">
              <div style="font-size:13px; font-weight:700; color:var(--ink);">{{ viz.headline }}</div>
              <div style="font-size:10px; color:var(--muted);">{{ viz.headlineSub }}</div>
            </div>
          </div>
          <div class="viz-col">

            <!-- analyse -->
            <div class="viz-card" style="display:flex; flex-direction:column; gap:9px;">
              <div class="viz-row">
                <span style="__CAPTION__">{{ viz.fromLabel }}</span>
                <input value="{{ viz.dir }}" sc-camel-on-change="{{ viz.onDir }}" placeholder="result directory, e.g. ntuanh/Optimizer/results/results_0729_2204_dynamic" style="__INPUT__">
                <button sc-camel-on-click="{{ viz.onBrowse }}" title="List this directory on the device" style="{{ viz.browseStyle }}">&#9776; browse</button>
              </div>
              <div style="{{ viz.browseRowStyle }}">
                <sc-for list="{{ viz.browseEntries }}" as="f" hint-placeholder-count="4">
                  <div sc-camel-on-click="{{ f.onPick }}" title="{{ f.path }}" style="{{ f.style }}">
                    <span style="flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">{{ f.name }}</span>
                    <span style="color:var(--muted); font-size:10px;" class="num">{{ f.size }}</span>
                  </div>
                </sc-for>
              </div>
              <!-- how much of the run to chart. The toggle is always here, so
                   a report's scope is a thing you can see before analysing;
                   the two boxes appear only once a slice has been asked for. -->
              <div class="viz-row">
                <span style="__CAPTION__">Window</span>
                <span class="viz-window">
                  <button sc-camel-on-click="{{ viz.onWindowToggle }}" title="{{ viz.windowToggleTitle }}" style="{{ viz.windowToggleStyle }}">{{ viz.windowToggleLabel }}</button>
                  <span class="viz-window" style="{{ viz.windowRowStyle }}">
                    <input value="{{ viz.windowStart }}" sc-camel-on-change="{{ viz.onWindowStart }}" placeholder="5" title="Where the window starts, as a percent of the run" style="__INPUT__ width:66px; text-align:right;">
                    <span class="viz-window-to">to</span>
                    <input value="{{ viz.windowEnd }}" sc-camel-on-change="{{ viz.onWindowEnd }}" placeholder="95" title="Where the window ends, as a percent of the run" style="__INPUT__ width:66px; text-align:right;">
                    <span class="viz-window-to">% of the run</span>
                  </span>
                </span>
              </div>
              <div style="{{ viz.windowHintStyle }}">{{ viz.windowHint }}</div>
              <div class="viz-row">
                <span style="__CAPTION__">Case test</span>
                <input value="{{ viz.caseName }}" sc-camel-on-change="{{ viz.onCaseName }}" placeholder="what is this run? e.g. cut6-8bit" style="__INPUT__">
                <button sc-camel-on-click="{{ viz.onAnalyze }}" disabled="{{ viz.busy }}" style="{{ viz.analyzeStyle }}">{{ viz.analyzeLabel }}</button>
              </div>
              <div style="{{ viz.statusStyle }}">{{ viz.status }}</div>
            </div>

            <!-- history: a day picker, then that day's runs -->
            <div class="viz-card" style="{{ viz.historyStyle }}">
              <div style="display:flex; gap:6px; align-items:center; flex-wrap:wrap;">
                <span style="__CAPTION__">History</span>
                <sc-for list="{{ viz.days }}" as="d" hint-placeholder-count="3">
                  <button sc-camel-on-click="{{ d.onPick }}" title="{{ d.title }}" style="{{ d.style }}">{{ d.label }}</button>
                </sc-for>
                <span style="flex:1; min-width:0;"></span>
                <button sc-camel-on-click="{{ viz.cmp.onToggle }}" title="{{ viz.cmp.toggleTitle }}" style="{{ viz.cmp.toggleStyle }}">{{ viz.cmp.toggleLabel }}</button>
              </div>
              <div style="{{ viz.runRowStyle }}">
                <sc-for list="{{ viz.runs }}" as="r" hint-placeholder-count="4">
                  <span class="viz-run" style="{{ r.wrapStyle }}">
                    <button class="viz-run-open" sc-camel-on-click="{{ r.onOpen }}" title="{{ r.title }}">{{ r.label }}</button>
                    <button class="viz-kill" sc-camel-on-click="{{ r.onDelete }}" title="{{ r.deleteTitle }}">&#10005;</button>
                  </span>
                </sc-for>
              </div>
            </div>

            <!-- compare: two or three reports, chart for chart -->
            <div class="viz-card viz-cmp" style="{{ viz.cmp.cardStyle }}">
              <div style="display:flex; align-items:center; gap:9px; flex-wrap:wrap;">
                <span style="__CAPTION__">Compare</span>
                <span style="flex:1; min-width:0; font-size:11px; color:var(--muted);">{{ viz.cmp.hint }}</span>
                <button sc-camel-on-click="{{ viz.cmp.onClear }}" title="Unpin every report" style="{{ viz.cmp.clearStyle }}">clear</button>
              </div>

              <!-- who is in which column, in the same grid as everything below -->
              <div class="viz-cmp-heads" style="{{ viz.cmp.headStyle }}">
                <span style="__CAPTION__">Reports</span>
                <sc-for list="{{ viz.cmp.slots }}" as="sl" hint-placeholder-count="2">
                  <div style="{{ sl.style }}">
                    <span style="{{ sl.dotStyle }}"></span>
                    <span style="flex:1; min-width:0; font-weight:700; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">{{ sl.name }}</span>
                    <span style="color:var(--muted); font-size:10px; white-space:nowrap;">{{ sl.when }}</span>
                    <button class="viz-kill" sc-camel-on-click="{{ sl.onRemove }}" title="{{ sl.removeTitle }}">&#10005;</button>
                  </div>
                </sc-for>
              </div>

              <div class="viz-cmp-empty" style="{{ viz.cmp.emptyStyle }}">{{ viz.cmp.emptyText }}</div>

              <!-- the headline numbers, matched by label -->
              <div class="viz-cmp-tiles" style="{{ viz.cmp.tilesStyle }}">
                <sc-for list="{{ viz.cmp.tiles }}" as="t" hint-placeholder-count="3">
                  <div class="viz-cmp-tile" style="{{ t.rowStyle }}">
                    <span class="viz-cmp-tile-label">{{ t.label }}</span>
                    <sc-for list="{{ t.cells }}" as="v" hint-placeholder-count="2">
                      <div style="min-width:0;">
                        <span class="viz-cmp-tile-value num" style="{{ v.valueStyle }}">{{ v.value }}<span class="viz-tile-unit">{{ v.unit }}</span></span>
                        <span class="viz-cmp-tile-delta" style="{{ v.deltaStyle }}">{{ v.delta }}</span>
                      </div>
                    </sc-for>
                  </div>
                </sc-for>
              </div>

              <!-- surf: pick one chart, or step through them -->
              <div class="viz-cmp-surf" style="{{ viz.cmp.pickerStyle }}">
                <button sc-camel-on-click="{{ viz.cmp.onPrev }}" title="Previous chart (&larr;)" style="{{ viz.cmp.stepStyle }}">&#9664;</button>
                <button sc-camel-on-click="{{ viz.cmp.onNext }}" title="Next chart (&rarr;)" style="{{ viz.cmp.stepStyle }}">&#9654;</button>
                <span class="num" style="font-size:10px; color:var(--muted); white-space:nowrap;">{{ viz.cmp.position }}</span>
                <div class="viz-cmp-chips">
                  <button sc-camel-on-click="{{ viz.cmp.onAll }}" title="Every chart, stacked" style="{{ viz.cmp.allStyle }}">&#9638; all</button>
                  <sc-for list="{{ viz.cmp.picker }}" as="p" hint-placeholder-count="4">
                    <button sc-camel-on-click="{{ p.onPick }}" title="{{ p.title }}" style="{{ p.style }}">{{ p.label }}</button>
                  </sc-for>
                </div>
              </div>

              <!-- one row per chart; one column per report -->
              <div style="{{ viz.cmp.rowsStyle }}">
                <sc-for list="{{ viz.cmp.rows }}" as="row" hint-placeholder-count="2">
                  <div class="viz-cmp-row">
                    <div class="viz-cmp-grid" style="{{ row.gridStyle }}">
                      <div class="viz-cmp-id">
                        <span class="viz-index">{{ row.index }}</span>
                        <span class="viz-cmp-title">{{ row.title }}</span>
                        <span class="viz-chip" style="{{ row.chipStyle }}">{{ row.kind }}</span>
                        <button class="viz-cmp-focus" sc-camel-on-click="{{ row.onFocus }}" title="{{ row.focusTitle }}">{{ row.focusLabel }}</button>
                      </div>
                      <sc-for list="{{ row.cells }}" as="cell" hint-placeholder-count="2">
                        <div class="viz-cmp-cell">
                          <div class="viz-cmp-tag" style="{{ cell.tagStyle }}">
                            <span style="{{ cell.dotStyle }}"></span><span>{{ cell.label }}</span>
                          </div>
                          <a href="{{ cell.src }}" target="_blank" rel="noopener" title="{{ cell.title }}" style="{{ cell.imgStyle }}">
                            <img src="{{ cell.src }}" alt="{{ cell.title }}" style="{{ cell.figStyle }}">
                          </a>
                          <div class="viz-cmp-miss" style="{{ cell.missStyle }}">{{ cell.missText }}</div>
                          <div class="viz-cmp-note" style="{{ cell.summaryStyle }}">{{ cell.summary }}</div>
                          <div class="viz-cmp-note" style="{{ cell.noteStyle }}">&#9998; {{ cell.note }}</div>
                        </div>
                      </sc-for>
                    </div>
                  </div>
                </sc-for>
              </div>
            </div>

            <!-- the numbers that are the story, before any chart -->
            <div class="viz-tiles" style="{{ viz.tileRowStyle }}">
              <sc-for list="{{ viz.tiles }}" as="t" hint-placeholder-count="4">
                <div class="viz-tile" title="{{ t.source }}">
                  <span style="{{ t.railStyle }}"></span>
                  <div class="viz-tile-label">{{ t.label }}</div>
                  <div class="viz-tile-value num">{{ t.value }}<span class="viz-tile-unit">{{ t.unit }}</span></div>
                  <div class="viz-tile-delta" style="{{ t.deltaStyle }}">{{ t.delta }}</div>
                </div>
              </sc-for>
            </div>

            <div style="{{ viz.emptyStyle }}">
              <div class="viz-empty">{{ viz.emptyText }}</div>
            </div>

            <!-- the gallery -->
            <div class="viz-gallery" style="{{ viz.galleryStyle }}">
              <sc-for list="{{ viz.charts }}" as="c" hint-placeholder-count="2">
                <div class="viz-chart" style="{{ c.cardStyle }}">
                  <div style="display:flex; align-items:center; gap:8px; margin-bottom:8px;">
                    <span class="viz-index">{{ c.index }}</span>
                    <span style="flex:1; min-width:0; font-size:12.5px; font-weight:700; color:var(--ink); overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">{{ c.title }}</span>
                    <span class="viz-chip" style="{{ c.chipStyle }}">{{ c.kind }}</span>
                    <button class="viz-gear" sc-camel-on-click="{{ c.onConfig }}" title="{{ c.configTitle }}" style="{{ c.gearStyle }}">&#9881;</button>
                  </div>
                  <div style="{{ c.subtitleStyle }}">{{ c.subtitle }}</div>

                  <!-- config: which series to draw, and what to call things.
                       The charts are server-rendered, so Apply re-draws them. -->
                  <div class="viz-config" style="{{ c.configStyle }}">
                    <div style="display:flex; flex-wrap:wrap; gap:6px; align-items:center;">
                      <span style="__CAPTION__">Show</span>
                      <sc-for list="{{ c.seriesList }}" as="s" hint-placeholder-count="2">
                        <button sc-camel-on-click="{{ s.onToggle }}" title="{{ s.title }}" style="{{ s.style }}">
                          <span style="{{ s.dotStyle }}"></span>{{ s.label }}
                        </button>
                      </sc-for>
                      <span style="{{ c.noSeriesStyle }}">this chart has no separate series</span>
                    </div>
                    <div style="display:grid; grid-template-columns:auto 1fr; gap:7px 9px; align-items:center; margin-top:10px;">
                      <span style="__CAPTION__">Title</span>
                      <input value="{{ c.draftTitle }}" sc-camel-on-change="{{ c.onTitle }}" placeholder="{{ c.titleHint }}" style="__INPUT__">
                      <span style="__CAPTION__">X axis</span>
                      <input value="{{ c.draftX }}" sc-camel-on-change="{{ c.onX }}" placeholder="{{ c.xHint }}" style="__INPUT__">
                      <span style="__CAPTION__">Y axis</span>
                      <input value="{{ c.draftY }}" sc-camel-on-change="{{ c.onY }}" placeholder="{{ c.yHint }}" style="__INPUT__">
                    </div>
                    <div style="display:flex; gap:7px; align-items:center; margin-top:10px; flex-wrap:wrap;">
                      <button sc-camel-on-click="{{ c.onApply }}" disabled="{{ viz.busy }}" style="{{ c.applyStyle }}">{{ c.applyLabel }}</button>
                      <button sc-camel-on-click="{{ c.onReset }}" title="Back to the chart as drawn by the guide" style="{{ c.resetStyle }}">Reset</button>
                      <span style="flex:1; min-width:0; font-size:10px; color:var(--muted);">{{ c.configHint }}</span>
                    </div>
                  </div>

                  <a href="{{ c.src }}" target="_blank" rel="noopener" title="Open this chart full size" style="display:block;">
                    <img src="{{ c.src }}" alt="{{ c.title }}" style="{{ c.imgStyle }}">
                  </a>
                  <div style="{{ c.summaryStyle }}">{{ c.summary }}</div>
                  <input class="viz-note" value="{{ c.note }}" sc-camel-on-change="{{ c.onNote }}" placeholder="short note &mdash; what does this chart tell you?">
                </div>
              </sc-for>
            </div>

            <!-- the overall review, and the save that makes it outlive the tab -->
            <div class="viz-card" style="{{ viz.saveRowStyle }}">
              <span style="__CAPTION__">Review</span>
              <input value="{{ viz.review }}" sc-camel-on-change="{{ viz.onReview }}" placeholder="what did this run show?" style="__INPUT__ flex:1;">
              <button sc-camel-on-click="{{ viz.onSave }}" style="{{ viz.saveStyle }}">{{ viz.saveLabel }}</button>
            </div>
          </div>
        </sc-if>
""".replace("__INPUT__", _VIZ_INPUT).replace("__CAPTION__", _VIZ_CAPTION)

# ------------------------------------------------------------------ patch 3/3
#: The Progress section: start the fleet schedule and watch it.
#:
#: A section of its own rather than a card on Control, because the two answer
#: different questions. Control is "run this command on those machines, show me
#: the output" -- a console, read line by line while you sit there. A schedule
#: is the opposite: hours long, unattended, and the only things worth seeing are
#: which project is up, how far in it is, and whether anything broke. That reads
#: as a status board, and a status board does not fit in a console.
#:
#: Every value here is filled by `live-patch.js` from /autorun/status plus the
#: `autorun_*` frames on /ws/stream, so the page is correct on load and stays
#: correct without polling.
PROGRESS_SECTION = """
        <!-- ============ PROGRESS ============ -->
        <sc-if value="{{ showProgress }}" hint-placeholder-val="{{ false }}">
          <div style="display:flex; align-items:flex-end; justify-content:space-between; gap:16px; margin-bottom:16px; flex-wrap:wrap;">
            <div>
              <h1 style="margin:0; font-size:20px; font-weight:800;">Progress</h1>
              <p style="margin:4px 0 0; font-size:13px; color:var(--muted); max-width:640px;">Run a schedule script on the fleet, unattended. Projects run one after another; each one's batch counter and FPS are read out of the server log as it goes, and failures reach Telegram the moment they happen.</p>
            </div>
            <div style="{{ prog.headlineStyle }}">
              <div style="font-size:13px; font-weight:700; color:var(--ink);">{{ prog.headline }}</div>
              <div style="font-size:10px; color:var(--muted);">{{ prog.headlineSub }}</div>
            </div>
          </div>

          <div class="prog-col">
            <!-- launch row -->
            <div class="prog-card" style="display:flex; gap:9px; align-items:center; flex-wrap:wrap;">
              <span style="__CAPTION__">Schedule</span>
              <select value="{{ prog.script }}" sc-camel-on-change="{{ prog.onScript }}" style="__INPUT__ flex:1; min-width:220px;">
                <sc-for list="{{ prog.scripts }}" as="s" hint-placeholder-count="2">
                  <option value="{{ s.name }}" selected="{{ s.selected }}">{{ s.name }}</option>
                </sc-for>
              </select>
              <button sc-camel-on-click="{{ prog.onRun }}" disabled="{{ prog.runDisabled }}" style="{{ prog.runStyle }}">{{ prog.runLabel }}</button>
              <button sc-camel-on-click="{{ prog.onStop }}" disabled="{{ prog.stopDisabled }}" style="{{ prog.stopStyle }}">&#9632; Stop</button>
              <span style="flex:1;"></span>
              <span title="{{ prog.notifyTitle }}" style="{{ prog.notifyStyle }}">{{ prog.notifyLabel }}</span>
            </div>

            <!-- per-project board: one row per step, live counters on the open one -->
            <div class="prog-card" style="{{ prog.boardStyle }}">
              <sc-for list="{{ prog.steps }}" as="p" hint-placeholder-count="3">
                <div style="{{ p.rowStyle }}">
                  <span style="{{ p.dotStyle }}"></span>
                  <span style="flex:1; min-width:0; font-size:13px; font-weight:700; color:var(--ink); overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">{{ p.name }}</span>
                  <span style="{{ p.metricStyle }}" class="num">{{ p.metrics }}</span>
                  <span style="font-size:11px; color:var(--muted); min-width:58px; text-align:right;" class="num">{{ p.duration }}</span>
                  <span style="{{ p.badgeStyle }}">{{ p.badge }}</span>
                </div>
                <div style="{{ p.barTrackStyle }}"><div style="{{ p.barFillStyle }}"></div></div>
              </sc-for>
              <div style="{{ prog.emptyStyle }}">Nothing has run yet. Pick a schedule and press Run &mdash; it keeps going after you close this tab.</div>
            </div>

            <!-- transcript -->
            <div class="prog-card" style="background:#0b1020; border-color:#1e293b;">
              <div style="display:flex; align-items:center; gap:8px; margin-bottom:9px;">
                <span style="font-size:11px; color:#94A3B8; font-family:ui-monospace,monospace;">{{ prog.logTitle }}</span>
                <span style="flex:1;"></span>
                <button sc-camel-on-click="{{ prog.onClear }}" style="background:none; border:none; color:#64748b; font-size:11px; cursor:pointer;">clear</button>
              </div>
              <div id="si-prog-log" style="max-height:330px; overflow:auto; display:flex; flex-direction:column; gap:2px; font-family:ui-monospace,monospace; font-size:12px; line-height:1.55;">
                <sc-for list="{{ prog.log }}" as="l" hint-placeholder-count="4">
                  <div style="color:{{ l.color }}; white-space:pre-wrap;">{{ l.text }}</div>
                </sc-for>
              </div>
            </div>
          </div>
        </sc-if>
""".replace("__INPUT__", _VIZ_INPUT).replace("__CAPTION__", _VIZ_CAPTION)

PROGRESS_CSS = """
  /* ---- Progress tab ---- */
  .prog-col { display:flex; flex-direction:column; gap:12px; }
  .prog-card {
    background:var(--raised); border:1px solid var(--border);
    border-radius:14px; padding:14px;
  }
  /* The bar is the only moving thing on the page, so it gets a transition --
     a batch counter that jumps looks like a glitch, one that slides reads as
     the run advancing. Width only; nothing here animates layout. */
  .prog-bar-fill { transition:width .45s ease; }
  @media (prefers-reduced-motion: reduce) { .prog-bar-fill { transition:none; } }
"""

#: Closes the `showMain` wrapper, i.e. the end of the last section. The Visual
#: and Progress sections go in ahead of it, so they sit after Control like
#: their nav items do.
SECTIONS_CLOSE = "\n      </sc-if>\n    </main>"


# ------------------------------------------------------------------ patch 2f/3
#: The left rail: sticky, and half width until the pointer is on it.
#:
#: The nav ships as a plain 212px flex child, which scrolls away with the
#: document (see the CSS note above) and holds that width whether or not anyone
#: is reading it. Both are fixed here in the markup, because the behaviour is a
#: containing-block arrangement -- a slot in the flow plus a panel that grows
#: over `<main>` -- and not something the inline styles can express.
RAIL_OPEN = ('<nav style="width:212px; flex-shrink:0; border-right:1px solid var(--border); '
             'background:var(--raised); padding:14px 12px; display:flex; '
             'flex-direction:column; gap:4px;">')
RAIL_OPEN_NEW = '<nav class="si-rail"><div class="si-rail-inner">'

#: The nav's close tag, matched together with the `<main>` that follows it so
#: the anchor cannot be confused with any other `</nav>`.
RAIL_CLOSE = '</nav><main style="flex:1; min-width:0; overflow:auto; padding:22px 26px;">'
RAIL_CLOSE_NEW = ('</div></nav>'
                  '<main style="flex:1; min-width:0; overflow:auto; padding:22px 26px;">')

#: Long labels ("Simulation") have to ellipsize at the collapsed width rather
#: than wrap the button onto two lines.
RAIL_LABEL = '<span style="flex:1; text-align:left;">{{ it.label }}</span>'
RAIL_LABEL_NEW = ('<span class="si-rail-label" style="flex:1; text-align:left;">'
                  '{{ it.label }}</span>')

RAIL_LEGEND = ('<div style="border-top:1px solid var(--border); padding-top:12px; '
               'margin-top:8px; display:flex; flex-direction:column; gap:7px;">')
RAIL_LEGEND_NEW = ('<div class="si-rail-legend" style="border-top:1px solid var(--border); '
                   'padding-top:12px; margin-top:8px; display:flex; '
                   'flex-direction:column; gap:7px;">')

RAIL_PATCHES = (
    ("nav opening tag", RAIL_OPEN, RAIL_OPEN_NEW),
    ("nav closing tag", RAIL_CLOSE, RAIL_CLOSE_NEW),
    ("nav item label", RAIL_LABEL, RAIL_LABEL_NEW),
    ("nav legend block", RAIL_LEGEND, RAIL_LEGEND_NEW),
)


# ------------------------------------------------------------------ patch 2c/3
#: The single fan-out console becomes one console per target: the focused one
#: full size, the rest as previews in a rail beside it.
#:
#: One shared console is unreadable the moment more than one machine answers --
#: twelve devices interleave their output and the only way to follow a single
#: one is to read every line. Splitting the streams is what makes fan-out
#: usable; the rail keeps the others visible so nothing goes unnoticed.
_DOTS = ('<span style="display:flex; gap:5px;">'
         '<span style="width:9px; height:9px; border-radius:50%; background:#f87171;"></span>'
         '<span style="width:9px; height:9px; border-radius:50%; background:#fbbf24;"></span>'
         '<span style="width:9px; height:9px; border-radius:50%; background:#4ade80;"></span></span>')
_SCROLLER = ('max-height:340px; overflow:auto; display:flex; flex-direction:column; '
             'gap:2px; font-family:ui-monospace,monospace; font-size:12px; line-height:1.55;')

CONSOLE_BLOCK = f"""<div style="background:#0b1020; border:1px solid var(--border); border-radius:14px; padding:14px; min-height:220px;">
                <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:10px;">
                  <div style="display:flex; align-items:center; gap:7px;">
                    {_DOTS}
                    <span style="font-size:11px; color:#94A3B8; font-family:ui-monospace,monospace;">fan-out console</span>
                  </div>
                  <button sc-camel-on-click="{{{{ ssh.onClear }}}}" style="background:none; border:none; color:#64748b; font-size:11px; cursor:pointer;">clear</button>
                </div>
                <div style="{_SCROLLER}">
                  <sc-for list="{{{{ ssh.out }}}}" as="o" hint-placeholder-count="4">
                    <div style="color:{{{{ o.color }}}}; white-space:pre-wrap;">{{{{ o.text }}}}</div>
                  </sc-for>
                </div>
              </div>"""

CONSOLE_MARKUP = f"""<div style="display:flex; gap:10px; align-items:flex-start;">
                <div style="flex:1; min-width:0; background:#0b1020; border:1px solid var(--border); border-radius:14px; padding:14px; min-height:220px;">
                  <div style="display:flex; align-items:center; gap:8px; margin-bottom:10px;">
                    {_DOTS}
                    <span style="font-size:11px; color:#94A3B8; font-family:ui-monospace,monospace;">{{{{ ssh.focusName }}}}</span>
                    <span style="flex:1;"></span>
                    <button sc-camel-on-click="{{{{ ssh.onClear }}}}" style="background:none; border:none; color:#64748b; font-size:11px; cursor:pointer;">clear</button>
                  </div>
                  <div id="si-console" style="{_SCROLLER}">
                    <sc-for list="{{{{ ssh.focusOut }}}}" as="o" hint-placeholder-count="4">
                      <div style="color:{{{{ o.color }}}}; white-space:pre-wrap;">{{{{ o.text }}}}</div>
                    </sc-for>
                  </div>
                </div>
                <div style="{{{{ ssh.railStyle }}}}">
                  <sc-for list="{{{{ ssh.miniConsoles }}}}" as="m" hint-placeholder-count="3">
                    <div sc-camel-on-click="{{{{ m.onClick }}}}" title="{{{{ m.title }}}}" style="{{{{ m.style }}}}">
                      <div style="display:flex; align-items:center; gap:6px; margin-bottom:3px;">
                        <span style="width:7px; height:7px; border-radius:50%; background:{{{{ m.dot }}}}; flex-shrink:0;"></span>
                        <span style="flex:1; font-size:10px; font-weight:700; color:var(--ink); overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">{{{{ m.name }}}}</span>
                        <span style="font-size:9px; color:var(--muted);" class="num">{{{{ m.count }}}}</span>
                      </div>
                      <sc-for list="{{{{ m.lines }}}}" as="l" hint-placeholder-count="2">
                        <div style="color:{{{{ l.color }}}}; font-size:9px; font-family:ui-monospace,monospace; line-height:1.5; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">{{{{ l.text }}}}</div>
                      </sc-for>
                    </div>
                  </sc-for>
                </div>
              </div>"""

#: End of the per-device connection form (⚙). A form you can fill but not act
#: on sends you hunting for "Connect all", which then dials every other device
#: too -- so the button belongs here, next to the fields it uses.
DEVICE_FORM_ANCHOR = '</label>\n                        </div>\n                      </sc-if>'

DEVICE_FORM_MARKUP = """</label>
                          <div style="grid-column:1 / -1; display:flex; gap:6px;">
                            <button sc-camel-on-click="{{ d.onCopy }}" title="Copy this device's login (Ctrl+C)" style="{{ d.copyStyle }}">&#10697; copy login</button>
                            <button sc-camel-on-click="{{ d.onPaste }}" title="{{ d.pasteTitle }}" style="{{ d.pasteStyle }}">{{ d.pasteLabel }}</button>
                          </div>
                          <button sc-camel-on-click="{{ d.onConnect }}" style="{{ d.connectStyle }}">{{ d.connectLabel }}</button>
                        </div>
                      </sc-if>"""

#: The stage header row (icon, name, kind selector, ✕). Anchored on the remove
#: button so the measure control lands to its left, at the end of the row.
#:
#: Per stage rather than per device because that is the unit bandwidth is
#: measured in: the backend times each machine alone so they cannot read each
#: other's share of the uplink, then times them all together, and the ratio
#: between the two only means something for a set of machines that share a
#: link. A stage is the closest thing this UI has to that set.
STAGE_HEADER_ANCHOR = '<button sc-camel-on-click="{{ stage.onRemove }}" title="Remove stage"'

STAGE_MEASURE_MARKUP = (
    '<button sc-camel-on-click="{{ stage.onMeasure }}" '
    'title="{{ stage.measureTitle }}" style="{{ stage.measureStyle }}">'
    "{{ stage.measureLabel }}</button>\n                  "
)

#: The stage-name box, which is what makes room for everything beside it.
#:
#: It is `flex:1`, so it should give up space as the row fills. It does not: an
#: `<input>` has an intrinsic minimum width (~20 characters), and the flexbox
#: default `min-width:auto` holds the item at that floor no matter what
#: `flex-basis` says. The row therefore grows past the card instead of the name
#: box getting narrower, and whatever sits at the end -- the ✕, and now the
#: measure button -- is pushed outside the border.
#:
#: `min-width:0` is what lets `flex-basis:0` actually take effect. With it the
#: name box absorbs the remainder and nothing else has to shrink at all, so the
#: buttons keep their natural size rather than being squeezed to "⟳ me".
STAGE_NAME_INPUT = (
    '<input value="{{ stage.name }}" sc-camel-on-change="{{ stage.onName }}" '
    'style="flex:1; background:transparent;'
)
STAGE_NAME_INPUT_NEW = (
    '<input value="{{ stage.name }}" sc-camel-on-change="{{ stage.onName }}" '
    'style="flex:1; min-width:0; background:transparent;'
)

#: Between the stage header and its device list -- where the result of the last
#: measurement is reported.
#:
#: The console already carries a line per device, but the console is a different
#: tab: someone who clicks ⟳ measure on the Stages tab watches the cards, not
#: the log, and a partial result (eight machines answered, one was unreachable)
#: is otherwise indistinguishable from a complete one. The notice says which,
#: next to the cards whose numbers it explains.
STAGE_NOTICE_ANCHOR = (
    '</div>\n                <div style="display:flex; flex-direction:column; gap:8px;">'
    '\n                  <sc-for list="{{ stage.devices }}"'
)

STAGE_NOTICE_MARKUP = (
    '</div>\n'
    '                <sc-if value="{{ stage.hasNotice }}" hint-placeholder-val="{{ false }}">'
    '<div style="{{ stage.noticeStyle }}">{{ stage.noticeText }}</div></sc-if>\n'
    '                <div style="display:flex; flex-direction:column; gap:8px;">'
    '\n                  <sc-for list="{{ stage.devices }}"'
)

CWD_MARKUP = """<div style="display:flex; gap:8px; align-items:center; margin-bottom:7px;">
                  <span style="font-size:10px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); font-weight:700;">Directory</span>
                  <input value="{{ ssh.cwd }}" sc-camel-on-change="{{ ssh.onCwd }}" placeholder="~  (every command runs here)" style="flex:1; background:var(--bg); border:1px solid var(--border); border-radius:8px; color:var(--ink); font-family:ui-monospace,monospace; font-size:12px; padding:6px 9px;">
                  <button sc-camel-on-click="{{ ssh.onEditPresets }}" title="Edit the command chips and saved directories" style="background:var(--bg); color:var(--muted); border:1px solid var(--border); border-radius:8px; padding:6px 11px; font-size:11px; font-weight:600; cursor:pointer;">&#9998; edit</button>
                </div>
                <div style="{{ ssh.dirRowStyle }}">
                  <sc-for list="{{ ssh.dirs }}" as="d" hint-placeholder-count="3">
                    <button sc-camel-on-click="{{ d.onPick }}" title="{{ d.path }}" style="{{ d.style }}">{{ d.label }}</button>
                  </sc-for>
                </div>
                """


# ------------------------------------------------------------------- patch 3/3
SCRIPT_OPEN = '<script type="text/x-dc"'
PATCH_BANNER = "\n\n/* ===== appended by tools/build_web.py from ui/live-patch.js ===== */\n"


def _patch_template(
    template: str,
    *,
    assets: dict[str, str],
    vendor: dict[str, str],
    live_patch: str,
    favicon: str | None,
) -> str:
    """Rewrite asset URLs, inject the header group and the live bridge."""
    html = template

    # --- assets: bundle uuids -> local files ---
    for uuid, path in assets.items():
        if uuid not in html:
            raise BuildError(f"asset {uuid} is in the manifest but not referenced")
        html = html.replace(uuid, path)

    # --- title + favicon: it is a page in a browser tab now, not a bundle ---
    # After the rewrite above, or the uuid inside the href gets substituted a
    # second time and the path doubles up.
    html = html.replace(
        "<head>",
        "<head>\n<title>Split Inference Studio</title>"
        + (f'\n<link rel="icon" href="{favicon}">' if favicon else ""),
        1,
    )

    # --- React before the runtime, then the transport layer ---
    # The DC runtime falls back to a CDN when `window.React` is missing; loading
    # the bundled copies first keeps the page working with no egress at all.
    runtime_tag = f'<script src="{vendor["runtime"]}"></script>'
    if runtime_tag not in html:
        raise BuildError("could not find the dc-runtime <script> tag")
    html = html.replace(
        runtime_tag,
        "\n".join(
            [
                f'<script src="{vendor["react"]}"></script>',
                f'<script src="{vendor["react_dom"]}"></script>',
                runtime_tag,
                '<script src="runtime-config.js"></script>',
                '<script src="backend-client.js"></script>',
            ]
        ),
        1,
    )

    # --- header group ---
    if html.count(HEADER_ANCHOR) != 1:
        raise BuildError(
            "header anchor (the theme-toggle button) is missing or ambiguous -- "
            "the UI's header changed; update HEADER_ANCHOR"
        )
    html = html.replace(HEADER_ANCHOR, HEADER_MARKUP + HEADER_ANCHOR, 1)

    # --- server card: collapsible, relabelled as SSH, AMQP row + jump toggle ---
    if html.count(SERVER_HEADER) != 1:
        raise BuildError(
            "server-card header anchor is missing or ambiguous -- update SERVER_HEADER"
        )
    html = html.replace(SERVER_HEADER, SERVER_HEADER_NEW, 1)

    for old, new in CARD_LABELS:
        if html.count(old) != 1:
            raise BuildError(
                f"server-card anchor is missing or ambiguous: {old[:60]!r} -- "
                "the UI's broker card changed; update CARD_LABELS"
            )
        html = html.replace(old, new, 1)

    if html.count(CARD_BUTTON) != 1:
        raise BuildError(
            "server-card anchor (the Test connection button) is missing or ambiguous -- "
            "update CARD_BUTTON"
        )
    html = html.replace(CARD_BUTTON, CARD_MARKUP + CARD_TAIL, 1)

    # --- select all, per stage rather than for the whole target list ---
    for old, new in ((TARGETS_SELECT_ALL, TARGETS_SELECT_ALL_NEW),
                     (GROUP_HEADER, GROUP_HEADER_NEW)):
        if html.count(old) != 1:
            raise BuildError(
                f"Targets-panel anchor is missing or ambiguous: {old[:60]!r} -- "
                "the UI's target list changed; update TARGETS_SELECT_ALL / "
                "GROUP_HEADER"
            )
        html = html.replace(old, new, 1)

    # --- working directory + preset editor, above the command line ---
    if html.count(CWD_ANCHOR) != 1:
        raise BuildError(
            "command-line anchor is missing or ambiguous -- update CWD_ANCHOR"
        )
    html = html.replace(CWD_ANCHOR, CWD_MARKUP + CWD_ANCHOR, 1)

    # --- a Connect button inside the per-device form ---
    if html.count(DEVICE_FORM_ANCHOR) != 1:
        raise BuildError(
            "device-form anchor is missing or ambiguous -- update DEVICE_FORM_ANCHOR"
        )
    html = html.replace(DEVICE_FORM_ANCHOR, DEVICE_FORM_MARKUP, 1)

    # --- a Measure button in each stage's header bar ---
    # The name box is made shrinkable first: adding a control to a row that
    # cannot give up space anywhere just pushes the end of the row out of the
    # card, taking the ✕ with it.
    if html.count(STAGE_NAME_INPUT) != 1:
        raise BuildError(
            "stage-name input is missing or ambiguous -- update STAGE_NAME_INPUT; "
            "without it the header row overflows the stage card"
        )
    html = html.replace(STAGE_NAME_INPUT, STAGE_NAME_INPUT_NEW, 1)

    if html.count(STAGE_HEADER_ANCHOR) != 1:
        raise BuildError(
            "stage-header anchor (the remove-stage button) is missing or ambiguous -- "
            "update STAGE_HEADER_ANCHOR"
        )
    html = html.replace(STAGE_HEADER_ANCHOR, STAGE_MEASURE_MARKUP + STAGE_HEADER_ANCHOR, 1)

    # --- the result line, between the header and the device cards ---
    if html.count(STAGE_NOTICE_ANCHOR) != 1:
        raise BuildError(
            "stage-notice anchor (the device list) is missing or ambiguous -- "
            "update STAGE_NOTICE_ANCHOR"
        )
    html = html.replace(STAGE_NOTICE_ANCHOR, STAGE_NOTICE_MARKUP, 1)

    # --- pull + browse, under the push row ---
    for old, new in ((SCP_TITLE, SCP_TITLE_NEW), (PULL_ANCHOR, PULL_MARKUP)):
        if html.count(old) != 1:
            raise BuildError(
                f"SCP-card anchor is missing or ambiguous: {old[:60]!r} -- update it"
            )
        html = html.replace(old, new, 1)

    # --- per-target consoles: focused one + a rail of previews ---
    if html.count(CONSOLE_BLOCK) != 1:
        raise BuildError(
            "console block is missing or ambiguous -- the UI's fan-out console "
            "changed; update CONSOLE_BLOCK"
        )
    html = html.replace(CONSOLE_BLOCK, CONSOLE_MARKUP, 1)

    # --- the left rail: sticky, collapsed until pointed at ---
    for what, old, new in RAIL_PATCHES:
        if html.count(old) != 1:
            raise BuildError(
                f"left-rail anchor is missing or ambiguous ({what}) -- the UI's "
                "sidebar changed; update RAIL_PATCHES"
            )
        html = html.replace(old, new, 1)

    # --- the Visual tab's stylesheet, appended to the page's own ---
    if html.count(CSS_ANCHOR) != 1:
        raise BuildError(
            "stylesheet anchor is missing or ambiguous -- the UI's <style> block "
            "changed; update CSS_ANCHOR"
        )
    html = html.replace(CSS_ANCHOR, CSS_ANCHOR + "\n" + VISUAL_CSS + PROGRESS_CSS, 1)

    # --- the Visual and Progress sections, last of the tabs ---
    if html.count(SECTIONS_CLOSE) != 1:
        raise BuildError(
            "sections anchor is missing or ambiguous -- the UI's tab wrapper "
            "changed; update SECTIONS_CLOSE"
        )
    html = html.replace(
        SECTIONS_CLOSE, VISUAL_SECTION + PROGRESS_SECTION + SECTIONS_CLOSE, 1
    )

    # --- live bridge, appended inside the logic script ---
    start = html.find(SCRIPT_OPEN)
    if start < 0:
        raise BuildError("no <script type=\"text/x-dc\"> logic block")
    end = html.find("</script>", start)
    if end < 0:
        raise BuildError("unterminated logic script")
    if "</script>" in live_patch:
        raise BuildError("live-patch.js contains a literal </script>")
    html = html[:end] + PATCH_BANNER + live_patch + "\n" + html[end:]

    return html


# ------------------------------------------------------------------------ build
def build(source: Path, out: Path) -> dict[str, str]:
    """Unpack + patch. Returns {relative path: sha256} for every file written."""
    if not source.exists():
        raise BuildError(f"UI bundle not found: {source}")
    html = source.read_text(encoding="utf-8")

    manifest = json.loads(_script_body(html, "__bundler/manifest"))
    template = json.loads(_script_body(html, "__bundler/template"))
    ext_resources = json.loads(_script_body(html, "__bundler/ext_resources"))

    # Which blob is which: ext_resources names the CDN scripts, so whatever
    # JavaScript is left over is the DC runtime itself.
    by_url = {r["uuid"]: r["id"] for r in ext_resources}
    react_uuid = react_dom_uuid = runtime_uuid = ""
    for uuid, url in by_url.items():
        if "react-dom" in url:
            react_dom_uuid = uuid
        elif "react" in url:
            react_uuid = uuid
    for uuid, entry in manifest.items():
        if uuid not in by_url and entry["mime"] in ("text/javascript", "application/javascript"):
            runtime_uuid = uuid
    missing = [n for n, v in
               (("react", react_uuid), ("react-dom", react_dom_uuid), ("dc-runtime", runtime_uuid))
               if not v]
    if missing:
        raise BuildError(f"bundle is missing expected scripts: {missing}")

    named = {react_uuid: "vendor/react.js",
             react_dom_uuid: "vendor/react-dom.js",
             runtime_uuid: "vendor/dc-runtime.js"}

    # Files are replaced in place and stale ones pruned afterwards, rather than
    # wiping the tree first: rebuilding while uvicorn is serving the directory
    # is routine, and on Windows an open handle makes rmtree fail outright.
    previous = _previous_files(out)
    (out / "vendor").mkdir(parents=True, exist_ok=True)
    (out / "assets").mkdir(parents=True, exist_ok=True)

    written: dict[str, str] = {}

    def write(rel: str, data: bytes) -> None:
        path = out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        written[rel] = hashlib.sha256(data).hexdigest()

    # The template references assets relative to itself and index.html sits at
    # the web root, so each relative path doubles as the URL. React and
    # react-dom are never referenced by the template -- the runtime asks for
    # them itself -- so only the runtime uuid joins the rewrite map.
    assets: dict[str, str] = {}
    for uuid, entry in manifest.items():
        rel = named.get(uuid) or f"assets/{uuid}{MIME_EXT.get(entry['mime'], '')}"
        write(rel, _decode_asset(entry))
        if uuid not in named or uuid == runtime_uuid:
            assets[uuid] = rel

    vendor = {"react": named[react_uuid],
              "react_dom": named[react_dom_uuid],
              "runtime": named[runtime_uuid]}

    # The header logo doubles as the tab icon.
    favicon = next(
        (rel for uuid, rel in assets.items() if manifest[uuid]["mime"].startswith("image/")),
        None,
    )

    live_patch = (UI_DIR / "live-patch.js").read_text(encoding="utf-8")
    page = _patch_template(
        template, assets=assets, vendor=vendor, live_patch=live_patch, favicon=favicon
    )
    write("index.html", page.encode("utf-8"))
    write("backend-client.js", (UI_DIR / "backend-client.js").read_bytes())

    # A build-time placeholder: the server replaces this route's body per
    # request so it can decide whether to hand out the API token (loopback
    # only). Written anyway so the directory is usable as plain static files.
    write(
        "runtime-config.js",
        b"window.__SPLIT_INFERENCE_BOOTSTRAP = "
        b'{"baseUrl": window.location.origin, "token": "", "served": false};\n',
    )

    for rel in _prune(out, previous, written):
        print(f"  pruned {rel}")

    (out / "BUILD.json").write_text(
        json.dumps(
            {
                "source": source.name,
                "source_sha256": hashlib.sha256(html.encode("utf-8")).hexdigest(),
                "files": written,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return written


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", type=Path, default=DEFAULT_SOURCE,
                    help=f"bundled UI HTML (default: {DEFAULT_SOURCE})")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT,
                    help=f"output directory (default: {DEFAULT_OUT})")
    ap.add_argument("--check", action="store_true",
                    help="exit non-zero if the existing build is missing or stale")
    args = ap.parse_args(argv)

    if args.check:
        record = args.out / "BUILD.json"
        if not record.exists():
            print(f"no build at {args.out} -- run: python tools/build_web.py", file=sys.stderr)
            return 1
        want = hashlib.sha256(args.source.read_bytes()).hexdigest()
        have = json.loads(record.read_text(encoding="utf-8")).get("source_sha256")
        if want != have:
            print("build is stale (UI bundle changed) -- rerun tools/build_web.py", file=sys.stderr)
            return 1
        print(f"build at {args.out} is current")
        return 0

    try:
        written = build(args.source, args.out)
    except BuildError as exc:
        print(f"build failed: {exc}", file=sys.stderr)
        return 2

    total = sum((args.out / rel).stat().st_size for rel in written)
    print(f"built {len(written)} files ({total / 1024:.0f} KiB) into {args.out}")
    for rel in sorted(written):
        print(f"  {rel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
