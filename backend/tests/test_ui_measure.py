"""`ui/live-patch.js` handlers, driven in a real JavaScript engine.

The patch is the one part of this repo that only ever runs in a browser, so it
is also the part that rots silently -- a renamed response field would show up as
a device card that quietly stops updating, with nothing in any log. These tests
load it with the page's globals stubbed and call the handlers directly.

Three groups. The **Measure** button, from the click through `/devices/measure`
to the numbers on the cards. The **✕ buttons** -- remove a stage, remove a
device, delete a saved report -- where the thing worth pinning is that they ask
first and that a cancelled prompt changes nothing, because none of what they
destroy is recoverable from the UI. And **Ctrl/Cmd+Enter**, which presses ▶ Run
without the mouse and so has to refuse everything the button refuses.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT / "tools"))

import build_web  # noqa: E402

quickjs = pytest.importorskip("quickjs", reason="pip install quickjs to run the UI tests")

LIVE_PATCH = BACKEND_ROOT / "ui" / "live-patch.js"

#: Everything `live-patch.js` touches at load time. It bails out early unless
#: `window.SplitInference` and a global `Component` are both present, so this is
#: the whole contract -- the rest of the page never runs here.
PRELUDE = """
var console = { warn: function () {}, log: function () {}, error: function () {} };
var CALLS = { patches: [], log: [] };
var LAST_NOTICE = null;

function Component() {}
Component.prototype = {
  componentDidMount: function () {}, componentDidUpdate: function () {},
  componentWillUnmount: function () {}, renderVals: function () { return {}; },
  simCluster: function () { return null; }, runSim: function () {},
  runFlow: function () {}, sshConnectAll: function () {}, sshRun: function () {},
  sshScp: function () {}, sshServerTest: function () {},
  removeStage: function (id) { REMOVED.push({ stage: id }); },
  removeDevice: function (stageId, id) { REMOVED.push({ stage: stageId, device: id }); }
};

var CONFIRMS = [];
var CONFIRM_ANSWER = true;

var window = {
  confirm: function (message) { CONFIRMS.push(message); return CONFIRM_ANSWER; },
  SplitInference: {
    config: { baseUrl: 'http://localhost:8000' },
    isLive: function () { return true; },
    configure: function () {},
    api: {}
  },
  addEventListener: function () {},
  localStorage: { getItem: function () { return null; }, setItem: function () {} },
  location: { origin: 'http://localhost:8000' }
};
var document = {
  addEventListener: function () {},
  createElement: function () { return { style: {}, appendChild: function () {} }; }
};

/* Chart images are object URLs the Visual tab owns and revokes. The handlers
   guard the call, but stubbing it keeps a real revoke from being mistaken for
   the "already gone" path. */
var REVOKED = [];
var URL = { revokeObjectURL: function (u) { REVOKED.push(u); } };
"""

#: A component instance that records what the handler did to it.
HARNESS = """
/* Inherits from the patched prototype, so a handler calling a sibling of its
   own (`self.siApplyMeasured`) finds the real one rather than a stub. */
function makeSelf(stage) {
  return Object.assign(Object.create(Component.prototype), {
    state: { stages: [stage], siMeasuring: {}, ssh: { out: [], outBy: {} } },
    setState: function (patch) {
      var next = typeof patch === 'function' ? patch(this.state) : patch;
      for (var k in next) this.state[k] = next[k];
    },
    sshLog: function (lines, deviceId) {
      lines.forEach(function (l) { CALLS.log.push({ text: l.text, device: deviceId || null }); });
    },
    updateDevice: function (stageId, id, patch) {
      CALLS.patches.push({ stage: stageId, id: id, patch: patch });
    },
    siSetNotice: function (stageId, notice) {
      CALLS.notice = notice; LAST_NOTICE = notice;
    },
    siSyncInventory: function () { return Promise.resolve(); }
  });
}

function applyMeasured(stage, response) {
  CALLS.patches = []; CALLS.log = [];
  var self = makeSelf(stage);
  Component.prototype.siApplyMeasured.call(self, stage.id, response);
  return JSON.stringify(CALLS);
}

/* The whole click path: button -> sync -> measureFleet -> cards.
 * `LAST` holds what the handler asked the API for, which is the part a
 * renamed request field would break without anything else noticing. */
/* The ✕ buttons. `REMOVED` records what the base implementation was asked to
   delete, so a cancelled confirm is visible as an empty list. */
var REMOVED = [];
function removeStage(stage, answer) {
  CONFIRMS = []; REMOVED = []; CONFIRM_ANSWER = answer;
  var self = makeSelf(stage);
  Component.prototype.removeStage.call(self, stage.id);
  return JSON.stringify({ confirms: CONFIRMS, removed: REMOVED,
                          measured: self.state.siMeasured || {} });
}

function removeDevice(stage, deviceId, answer) {
  CONFIRMS = []; REMOVED = []; CONFIRM_ANSWER = answer;
  var self = makeSelf(stage);
  Component.prototype.removeDevice.call(self, stage.id, deviceId);
  return JSON.stringify({ confirms: CONFIRMS, removed: REMOVED });
}

/* Deleting a saved report. `DELETED` records what reached the API, so a
   cancelled confirm is visible as an empty list rather than as a request that
   happened to fail. */
var DELETED = [];
var RELISTED = false;
function deleteReport(saved, openId, answer) {
  CONFIRMS = []; DELETED = []; RELISTED = false; CONFIRM_ANSWER = answer;
  var self = Object.assign(Object.create(Component.prototype), {
    state: {
      viz: {
        saved: saved,
        report: openId ? { id: openId, charts: [] } : null,
        srcs: { chart: 'blob:one' }, notes: { chart: 'a note' },
        review: 'a review', drafts: { chart: {} }, configFor: 'chart'
      },
      ssh: { out: [], outBy: {} }
    },
    setState: function (patch) {
      var next = typeof patch === 'function' ? patch(this.state) : patch;
      for (var k in next) this.state[k] = next[k];
    },
    sshLog: function () {},
    vizLoadSaved: function () { RELISTED = true; return Promise.resolve(); }
  });
  window.SplitInference.api.deleteReport = function (id) {
    DELETED.push(id);
    return Promise.resolve({ deleted: id });
  };
  var p = Component.prototype.vizDelete.call(self, saved[0].id);
  return function () {
    return JSON.stringify({
      confirms: CONFIRMS, deleted: DELETED, relisted: RELISTED,
      viz: self.state.viz, returned: !!(p && p.then)
    });
  };
}

var LAST = null;
function clickMeasure(stage, response, opts) {
  CALLS.patches = []; CALLS.log = []; LAST = null;
  var self = makeSelf(stage);
  self.siSyncInventory = function () { CALLS.synced = true; return Promise.resolve(); };
  if (opts && opts.busy) self.state.siMeasuring = { s1: true };
  window.SplitInference.api.measureFleet = function (body) {
    LAST = body;
    return response === null ? Promise.reject(new Error('backend offline'))
                             : Promise.resolve(response);
  };
  var done = false;
  var p = Component.prototype.siMeasureStage.call(self, stage.id);
  if (p && p.then) p.then(function () { done = true; }); else done = true;
  return function () {
    return JSON.stringify({
      calls: CALLS, request: LAST, done: done,
      measuring: Object.keys(self.state.siMeasuring || {})
    });
  };
}

/* Ctrl/Cmd+Enter in the command box. `RAN` says whether the keystroke reached
   `sshRun`, and `PREVENTED` whether it took the browser's own handling away
   from a keystroke that then did nothing. The event defaults to the real one --
   Ctrl+Enter, from an input holding exactly what the box holds -- so each test
   overrides only the part it is about. */
var RAN = 0;
var PREVENTED = 0;
function runKey(ssh, event) {
  RAN = 0; PREVENTED = 0;
  var self = Object.assign(Object.create(Component.prototype), {
    state: { ssh: ssh },
    sshRun: function () { RAN++; }
  });
  var e = Object.assign({
    key: 'Enter', ctrlKey: true,
    target: { tagName: 'INPUT', value: ssh.command }
  }, event || {});
  e.preventDefault = function () { PREVENTED++; };
  var fired = Component.prototype.siRunKey.call(self, e);
  return JSON.stringify({ fired: fired, ran: RAN, prevented: PREVENTED });
}
"""

STAGE = {
    "id": "s1",
    "name": "Edge",
    "devices": [{"id": "dA", "name": "machine 2"}, {"id": "dB", "name": "machine 3"}],
}


@pytest.fixture(scope="module")
def js():
    ctx = quickjs.Context()
    ctx.eval(PRELUDE)
    ctx.eval(LIVE_PATCH.read_text(encoding="utf-8"))
    ctx.eval(HARNESS)
    # The patch installs onto Component.prototype; if the early bail-out fired,
    # nothing below would exist and every test would fail for the wrong reason.
    assert ctx.eval("typeof Component.prototype.siApplyMeasured") == "function"
    return ctx


def run(js, response: dict) -> dict:
    out = js.eval(
        f"applyMeasured({json.dumps(STAGE)}, {json.dumps(response)})"
    )
    return json.loads(out)


def log_text(calls: dict) -> str:
    return "\n".join(entry["text"] for entry in calls["log"])


def click(js, response: dict | None, **opts) -> dict:
    """Press the button and let the promise chain drain."""
    js.eval(
        f"var _read = clickMeasure({json.dumps(STAGE)}, "
        f"{json.dumps(response)}, {json.dumps(opts)});"
    )
    for _ in range(200):          # pump the microtask queue to quiescence
        if not js.execute_pending_job():
            break
    return json.loads(js.eval("_read()"))


# ------------------------------------------------------------------- applying
def test_a_measurement_replaces_all_three_specs(js):
    calls = run(js, {
        "results": [{
            "device_id": "dA", "device_name": "machine 2", "ok": True,
            "gflops": 1487.62, "bandwidth_mb_s": 112.4372, "latency_ms": 0.8461,
            "sources": {"gflops": "conv-fp32", "bandwidth": "sftp-blob",
                        "latency": "tcp-connect"},
            "warnings": [],
        }],
        "summary": {"measured": 1, "contention_pass": False},
    })

    assert calls["patches"] == [{
        "stage": "s1", "id": "dA",
        # Rounded for the card's number inputs, not truncated to integers.
        "patch": {"gflops": 1487.6, "bw": 112.4, "lat": 0.85},
    }]
    assert "conv-fp32 · sftp-blob · tcp-connect" in log_text(calls)


def test_an_unmeasurable_field_is_left_alone_rather_than_zeroed(js):
    """A device without torch keeps the GFLOPS someone typed."""
    calls = run(js, {
        "results": [{
            "device_id": "dA", "device_name": "machine 2", "ok": True,
            "gflops": None, "bandwidth_mb_s": 90.0, "latency_ms": 2.0,
            "sources": {"bandwidth": "sftp-blob", "latency": "tcp-connect"},
            "warnings": ["on-device benchmark failed (no module named torch)"],
        }],
        "summary": {},
    })

    assert calls["patches"][0]["patch"] == {"bw": 90.0, "lat": 2.0}
    assert "gflops" not in calls["patches"][0]["patch"]
    assert "benchmark failed" in log_text(calls)


def test_an_unreachable_device_is_reported_and_never_written(js):
    calls = run(js, {
        "results": [
            {"device_id": "dA", "device_name": "machine 2", "ok": False,
             "error": "cannot reach 10.0.1.10"},
            {"device_id": "dB", "device_name": "machine 3", "ok": True,
             "gflops": 472.0, "bandwidth_mb_s": 12.0, "latency_ms": 6.0,
             "sources": {}, "warnings": []},
        ],
        "summary": {"devices": 2, "measured": 1},
    })

    assert [p["id"] for p in calls["patches"]] == ["dB"]
    text = log_text(calls)
    assert "cannot reach 10.0.1.10" in text
    assert "1/2 device(s) updated" in text


def test_warnings_are_never_swallowed(js):
    """They are the difference between a spec you can trust and an estimate."""
    calls = run(js, {
        "results": [{
            "device_id": "dA", "device_name": "machine 2", "ok": True,
            "gflops": 8100.0, "bandwidth_mb_s": 125.0, "latency_ms": 2.0,
            "sources": {"gflops": "vendor-peak", "bandwidth": "nic-link"},
            "warnings": ["gflops estimated from vendor peak FP32",
                         "bandwidth taken from the NIC link speed"],
        }],
        "summary": {},
    })
    text = log_text(calls)
    assert "vendor peak FP32" in text
    assert "NIC link speed" in text
    assert "vendor-peak · nic-link" in text


def test_the_contention_result_is_summarised_for_the_operator(js):
    calls = run(js, {
        "results": [{
            "device_id": "dA", "ok": True, "gflops": 500.0,
            "bandwidth_mb_s": 31.8, "latency_ms": 3.0,
            "sources": {}, "warnings": [],
        }],
        "summary": {
            "measured": 1, "contention_pass": True,
            "worst_contention_ratio": 0.283, "aggregate_shared_mb_s": 127.4,
        },
    })
    text = log_text(calls)
    assert "28%" in text
    assert "127 MB/s across the group" in text


def test_a_reachable_device_with_nothing_measurable_is_not_silently_skipped(js):
    calls = run(js, {
        "results": [{"device_id": "dA", "device_name": "machine 2", "ok": True,
                     "sources": {}, "warnings": []}],
        "summary": {},
    })
    assert calls["patches"] == []
    assert "nothing could be measured" in log_text(calls)
    assert "0/1 device(s) updated" in log_text(calls)


def test_an_empty_response_says_so_instead_of_claiming_success(js):
    calls = run(js, {"results": [], "summary": {}})
    assert calls["patches"] == []
    assert "nothing was measured" in log_text(calls)


# ------------------------------------------------------------- the click path
GOOD_RESPONSE = {
    "results": [
        {"device_id": "dA", "device_name": "machine 2", "ok": True, "gflops": 472.0,
         "bandwidth_mb_s": 12.0, "latency_ms": 6.0, "sources": {}, "warnings": []},
        {"device_id": "dB", "device_name": "machine 3", "ok": True, "gflops": 384.0,
         "bandwidth_mb_s": 10.0, "latency_ms": 8.0, "sources": {}, "warnings": []},
    ],
    "summary": {"devices": 2, "measured": 2, "contention_pass": True,
                "worst_contention_ratio": 0.9, "aggregate_shared_mb_s": 22.0},
    "applied": True, "bandwidth_basis": "shared",
}


def test_clicking_measure_asks_for_exactly_this_stage_and_writes_the_answer(js):
    out = click(js, GOOD_RESPONSE)

    # The request shape the backend actually expects.
    assert out["request"] == {
        "device_ids": ["dA", "dB"], "apply": True, "contention": True
    }
    # Inventory is pushed first -- a device added since the last sync has no
    # row on the server, and measuring it would 404.
    assert out["calls"]["synced"] is True
    assert [(p["id"], p["patch"]) for p in out["calls"]["patches"]] == [
        ("dA", {"gflops": 472.0, "bw": 12.0, "lat": 6.0}),
        ("dB", {"gflops": 384.0, "bw": 10.0, "lat": 8.0}),
    ]
    assert out["measuring"] == [], "the button was left spinning"


def test_a_failed_call_reports_it_and_releases_the_button(js):
    out = click(js, None)
    assert out["calls"]["patches"] == []
    assert "backend offline" in log_text(out["calls"])
    assert out["measuring"] == [], "a failed measure left the button spinning"


def test_a_second_click_while_it_is_running_is_ignored(js):
    """The pass takes minutes on a full stage; queueing a second one behind it
    would double the traffic the contention pass is trying to measure."""
    out = click(js, GOOD_RESPONSE, busy=True)
    assert out["request"] is None
    assert out["calls"]["patches"] == []


# ---------------------------------------------------------------- the button
def test_the_stage_header_gains_a_measure_button() -> None:
    if not build_web.DEFAULT_SOURCE.exists():
        pytest.skip("UI bundle not present")
    src = build_web.DEFAULT_SOURCE.read_text(encoding="utf-8")
    template = json.loads(build_web._script_body(src, "__bundler/template"))

    # One stage header in the bundle, and no measure button before the build.
    assert template.count(build_web.STAGE_HEADER_ANCHOR) == 1
    assert "stage.onMeasure" not in template

    patched = template.replace(
        build_web.STAGE_HEADER_ANCHOR,
        build_web.STAGE_MEASURE_MARKUP + build_web.STAGE_HEADER_ANCHOR, 1,
    )
    # Immediately before the remove button, i.e. at the end of the header row.
    assert patched.index("stage.onMeasure") < patched.index("stage.onRemove")
    assert "stage.measureLabel" in patched


def test_the_stage_name_box_is_made_shrinkable() -> None:
    """Otherwise the button lands outside the card.

    The name box is `flex:1`, but an `<input>` carries an intrinsic minimum
    width and flexbox's default `min-width:auto` pins it there -- so the header
    row grows past the stage card rather than the name giving up space, and
    everything after it (the ✕, and the measure button) is pushed out of the
    border. This is the line that lets `flex-basis:0` actually apply.
    """
    if not build_web.DEFAULT_SOURCE.exists():
        pytest.skip("UI bundle not present")
    src = build_web.DEFAULT_SOURCE.read_text(encoding="utf-8")
    template = json.loads(build_web._script_body(src, "__bundler/template"))

    assert template.count(build_web.STAGE_NAME_INPUT) == 1
    assert "min-width:0" not in build_web.STAGE_NAME_INPUT
    assert "min-width:0" in build_web.STAGE_NAME_INPUT_NEW


def test_the_built_page_keeps_the_header_row_inside_the_card() -> None:
    """The end-to-end version of the test above, against real build output."""
    web = BACKEND_ROOT / "web" / "index.html"
    if not web.is_file():
        pytest.skip("site not built in this checkout (run tools/build_web.py)")
    html = web.read_text(encoding="utf-8")

    # From the name box to the end of the header row -- everything that has to
    # share one line inside the card.
    start = html.index("{{ stage.name }}")
    row = html[start:html.index("</div>", html.index("stage.onRemove", start))]

    assert "min-width:0" in row[:400], "the stage-name box cannot shrink"
    # ...and the two controls that would otherwise be pushed out are both in
    # that row, in the order the design calls for.
    assert row.index("stage.onMeasure") < row.index("stage.onRemove")


def test_the_measure_button_refuses_to_shrink() -> None:
    """If it shrank, the row would resolve the overflow by squeezing the label
    to "⟳ me" instead of narrowing the name box, which is what the screenshot
    showed before the fix."""
    patch = (BACKEND_ROOT / "ui" / "live-patch.js").read_text(encoding="utf-8")
    start = patch.index("measureStyle: {")
    assert "flexShrink: 0" in patch[start:start + 700]


def test_build_fails_loudly_when_the_stage_name_input_moves() -> None:
    if not build_web.DEFAULT_SOURCE.exists():
        pytest.skip("UI bundle not present")
    src = build_web.DEFAULT_SOURCE.read_text(encoding="utf-8")
    template = json.loads(build_web._script_body(src, "__bundler/template"))
    broken = template.replace(build_web.STAGE_NAME_INPUT, '<input data-renamed style="')

    import re
    runtime_uuid = re.search(r'<script src="([0-9a-f-]{36})">', template).group(1)
    with pytest.raises(build_web.BuildError, match="stage-name input"):
        build_web._patch_template(
            broken,
            assets={},
            vendor={"react": "r.js", "react_dom": "rd.js", "runtime": runtime_uuid},
            live_patch="",
            favicon=None,
        )


def test_build_fails_loudly_when_the_stage_header_moves() -> None:
    """A UI re-export that renames the remove-stage button must not produce a
    site whose Measure button silently vanished."""
    if not build_web.DEFAULT_SOURCE.exists():
        pytest.skip("UI bundle not present")
    src = build_web.DEFAULT_SOURCE.read_text(encoding="utf-8")
    template = json.loads(build_web._script_body(src, "__bundler/template"))
    broken = template.replace(build_web.STAGE_HEADER_ANCHOR, "<button data-renamed")

    import re
    runtime_uuid = re.search(r'<script src="([0-9a-f-]{36})">', template).group(1)
    with pytest.raises(build_web.BuildError, match="stage-header anchor"):
        build_web._patch_template(
            broken,
            assets={},
            vendor={"react": "r.js", "react_dom": "rd.js", "runtime": runtime_uuid},
            live_patch="",
            favicon=None,
        )


# --------------------------------------------------------- the stage notice
def test_the_stage_says_which_devices_measured(js):
    calls = run(js, {
        "results": [
            {"device_id": "dA", "device_name": "machine 2", "ok": True, "gflops": 65.1,
             "bandwidth_mb_s": 46.7, "latency_ms": 0.43, "sources": {}, "warnings": []},
            {"device_id": "dB", "device_name": "machine 3", "ok": True, "gflops": 63.6,
             "bandwidth_mb_s": 41.2, "latency_ms": 0.47, "sources": {}, "warnings": []},
        ],
        "summary": {},
    })
    assert calls["notice"]["ok"] == ["machine 2", "machine 3"]
    assert calls["notice"]["failed"] == []
    assert calls["notice"]["total"] == 2

    n = json.loads(js.eval("JSON.stringify(Component.prototype.siNoticeFor(LAST_NOTICE, false))"))
    assert n["hasNotice"] is True
    # Names the fields, not just the device count.
    assert n["noticeText"] == "✓ measured 2/2 · GFLOPS, MB/s, LAT MS"
    assert "--data" in n["noticeStyle"]["color"]      # green: everything landed


def test_the_stage_names_the_devices_that_failed(js):
    calls = run(js, {
        "results": [
            {"device_id": "dA", "device_name": "machine 2", "ok": True, "gflops": 65.1,
             "sources": {}, "warnings": []},
            {"device_id": "dB", "device_name": "machine 3", "ok": False,
             "error": "timeout after 60s"},
        ],
        "summary": {},
    })
    assert calls["notice"]["ok"] == ["machine 2"]
    assert calls["notice"]["failed"] == ["machine 3"]

    n = json.loads(js.eval("JSON.stringify(Component.prototype.siNoticeFor(LAST_NOTICE, false))"))
    assert n["noticeText"].startswith("✓ measured 1/2 · GFLOPS")
    assert "skipped: machine 3" in n["noticeText"]
    assert "--server" in n["noticeStyle"]["color"]      # amber: partial


def test_reachable_but_unmeasured_does_not_count_as_success(js):
    """A tick next to a card whose numbers nothing touched would be a lie."""
    calls = run(js, {
        "results": [{"device_id": "dA", "device_name": "machine 2", "ok": True,
                     "sources": {}, "warnings": []}],
        "summary": {},
    })
    assert calls["notice"]["ok"] == []
    assert calls["notice"]["failed"] == ["machine 2"]

    n = json.loads(js.eval("JSON.stringify(Component.prototype.siNoticeFor(LAST_NOTICE, false))"))
    assert n["noticeText"] == "✗ none of 1 measured: machine 2"
    assert "--alert" in n["noticeStyle"]["color"]      # red: nothing landed


def test_a_long_stage_truncates_the_name_list(js):
    results = [
        {"device_id": f"d{i}", "device_name": f"machine {i}", "ok": True,
         "gflops": 50.0, "sources": {}, "warnings": []}
        for i in range(9)
    ]
    run(js, {"results": results, "summary": {}})
    n = json.loads(js.eval("JSON.stringify(Component.prototype.siNoticeFor(LAST_NOTICE, false))"))
    assert n["noticeText"].startswith("✓ measured 9/9 · GFLOPS")


def test_the_notice_shows_progress_while_it_runs(js):
    n = json.loads(js.eval("JSON.stringify(Component.prototype.siNoticeFor(null, true))"))
    assert n["hasNotice"] is True
    assert "measuring" in n["noticeText"]
    # ...and says nothing at all before the first run.
    idle = json.loads(js.eval("JSON.stringify(Component.prototype.siNoticeFor(null, false))"))
    assert idle["hasNotice"] is False


def test_the_stage_notice_is_wired_into_the_page() -> None:
    web = BACKEND_ROOT / "web" / "index.html"
    if not web.is_file():
        pytest.skip("site not built in this checkout")
    html = web.read_text(encoding="utf-8")
    # Between the header row and the device cards.
    assert html.index("stage.onRemove") < html.index("stage.hasNotice")
    assert html.index("stage.hasNotice") < html.index("{{ stage.devices }}")


def test_build_fails_loudly_when_the_notice_anchor_moves() -> None:
    if not build_web.DEFAULT_SOURCE.exists():
        pytest.skip("UI bundle not present")
    src = build_web.DEFAULT_SOURCE.read_text(encoding="utf-8")
    template = json.loads(build_web._script_body(src, "__bundler/template"))
    broken = template.replace(build_web.STAGE_NOTICE_ANCHOR, "<div data-renamed>")

    import re
    runtime_uuid = re.search(r'<script src="([0-9a-f-]{36})">', template).group(1)
    with pytest.raises(build_web.BuildError, match="stage-notice anchor"):
        build_web._patch_template(
            broken,
            assets={},
            vendor={"react": "r.js", "react_dom": "rd.js", "runtime": runtime_uuid},
            live_patch="",
            favicon=None,
        )


# -------------------------------------------------------------- the ✕ buttons
FULL_STAGE = {
    "id": "s1",
    "name": "Edge",
    "devices": [
        {"id": "dA", "name": "machine 2"},
        {"id": "dB", "name": "machine 3"},
        {"id": "dC", "name": "machine 4"},
    ],
}


def test_removing_a_stage_asks_first_and_says_what_goes_with_it(js):
    out = json.loads(js.eval(f"removeStage({json.dumps(FULL_STAGE)}, true)"))

    assert len(out["confirms"]) == 1
    message = out["confirms"][0]
    assert 'Remove the "Edge" stage?' in message
    # The part a bare ✕ does not tell you: the devices go too, and so do their
    # credentials.
    assert "3 device(s) go with it" in message
    assert "machine 2, machine 3, machine 4" in message
    assert "stored SSH passwords are deleted" in message

    assert out["removed"] == [{"stage": "s1"}]


def test_cancelling_the_stage_prompt_removes_nothing(js):
    out = json.loads(js.eval(f"removeStage({json.dumps(FULL_STAGE)}, false)"))
    assert len(out["confirms"]) == 1
    assert out["removed"] == [], "the stage was deleted after the prompt was cancelled"


def test_removing_a_device_asks_first(js):
    out = json.loads(js.eval(f"removeDevice({json.dumps(FULL_STAGE)}, 'dB', true)"))
    assert len(out["confirms"]) == 1
    assert 'Remove "machine 3"?' in out["confirms"][0]
    assert "stored SSH password are deleted" in out["confirms"][0]
    assert out["removed"] == [{"stage": "s1", "device": "dB"}]


def test_cancelling_the_device_prompt_removes_nothing(js):
    out = json.loads(js.eval(f"removeDevice({json.dumps(FULL_STAGE)}, 'dB', false)"))
    assert out["removed"] == []


def test_an_empty_stage_still_asks_but_does_not_claim_devices(js):
    empty = {"id": "s1", "name": "Fog", "devices": []}
    out = json.loads(js.eval(f"removeStage({json.dumps(empty)}, true)"))
    assert 'Remove the "Fog" stage?' in out["confirms"][0]
    assert "device(s) go with it" not in out["confirms"][0]
    assert out["removed"] == [{"stage": "s1"}]


# ----------------------------------------- the history bar's ✕, same contract
#: Two saved reports as `/reports` lists them. The first is the one deleted.
SAVED = [
    {"id": "3007-0918_split", "case_name": "split", "label": "30 Jul 09:18",
     "charts": 10, "notes": 2, "reviewed": True, "day": "2026-07-30"},
    {"id": "2907-2147_dynamic", "case_name": "dynamic", "label": "29 Jul 21:47",
     "charts": 10, "notes": 0, "reviewed": False, "day": "2026-07-29"},
]


def delete_report(js, *, answer: bool, open_id: str = "") -> dict:
    js.eval(
        f"var _read = deleteReport({json.dumps(SAVED)}, "
        f"{json.dumps(open_id)}, {json.dumps(answer)});"
    )
    for _ in range(200):          # pump the microtask queue to quiescence
        if not js.execute_pending_job():
            break
    return json.loads(js.eval("_read()"))


def test_deleting_a_report_asks_first_and_says_what_goes(js):
    out = delete_report(js, answer=True)

    assert len(out["confirms"]) == 1
    message = out["confirms"][0]
    assert 'Delete the report "split" from 30 Jul 09:18?' in message
    # The count of what is lost is the point of asking at all.
    assert "10 chart(s) and 2 note(s) go with it" in message
    assert "cannot be undone" in message
    assert out["deleted"] == ["3007-0918_split"]
    # Re-listed rather than spliced locally: the day chips carry counts, and
    # deleting the last report of a day has to drop its chip too.
    assert out["relisted"] is True


def test_cancelling_the_report_prompt_deletes_nothing(js):
    out = delete_report(js, answer=False)

    assert len(out["confirms"]) == 1
    assert out["deleted"] == [], "the report was deleted after the prompt was cancelled"
    assert out["relisted"] is False
    # Nothing on screen moved either.
    assert out["viz"]["review"] == "a review"


def test_deleting_the_open_report_clears_the_gallery(js):
    """Otherwise the tab shows charts whose backing folder no longer exists."""
    out = delete_report(js, answer=True, open_id="3007-0918_split")

    assert out["deleted"] == ["3007-0918_split"]
    assert out["viz"]["report"] is None
    assert out["viz"]["srcs"] == {}
    assert out["viz"]["notes"] == {}
    assert out["viz"]["review"] == ""
    # …and its object URLs were released rather than left to leak.
    assert json.loads(js.eval("JSON.stringify(REVOKED)")) == ["blob:one"]


def test_deleting_another_report_leaves_the_open_one_alone(js):
    out = delete_report(js, answer=True, open_id="2907-2147_dynamic")

    assert out["deleted"] == ["3007-0918_split"]
    assert out["viz"]["report"]["id"] == "2907-2147_dynamic"
    assert out["viz"]["srcs"] == {"chart": "blob:one"}
    assert out["viz"]["review"] == "a review"


def test_a_report_with_no_notes_is_not_described_as_having_them(js):
    js.eval("var _bare = " + json.dumps([dict(SAVED[0], notes=0, reviewed=False)]))
    js.eval("var _read = deleteReport(_bare, '', true);")
    for _ in range(200):
        if not js.execute_pending_job():
            break
    message = json.loads(js.eval("_read()"))["confirms"][0]

    assert "10 chart(s) go with it" in message
    assert "note(s)" not in message


def test_removing_a_stage_drops_its_measurement_notice(js):
    """Otherwise a later stage could inherit a stale "measured 9/9" line."""
    js.eval(f"""
      var _self = makeSelf({json.dumps(FULL_STAGE)});
      _self.state.siMeasured = {{ s1: {{ ok: ['machine 2'], failed: [], total: 1 }} }};
      _self.state.siMeasuring = {{ s1: true }};
      CONFIRM_ANSWER = true;
      Component.prototype.removeStage.call(_self, 's1');
    """)
    left = json.loads(js.eval("JSON.stringify(_self.state.siMeasured)"))
    still_busy = json.loads(js.eval("JSON.stringify(_self.state.siMeasuring)"))
    assert left == {}
    assert still_busy == {}


# --------------------------------------- the notice must not overstate itself
def test_a_skipped_field_is_named_rather_than_counted_as_success(js):
    """The bug this replaced: a stage where bandwidth was skipped on every
    machine still said "measured 3/3", so the untouched MB/S on the cards read
    as a fresh measurement."""
    results = [
        {"device_id": f"d{i}", "device_name": f"device-{i}", "ok": True,
         "gflops": 120.0, "bandwidth_mb_s": None, "latency_ms": 0.4,
         "sources": {"bandwidth": "skipped (jump host)"},
         "warnings": ["bandwidth not measured: ... jump host ..."]}
        for i in range(1, 4)
    ]
    run(js, {"results": results, "summary": {}})
    n = json.loads(js.eval("JSON.stringify(Component.prototype.siNoticeFor(LAST_NOTICE, false))"))

    assert n["noticeText"] == "✓ measured 3/3 · GFLOPS, LAT MS · MB/s not measured"
    assert "--server" in n["noticeStyle"]["color"], "a partial result must not look green"


def test_a_field_measured_on_only_some_devices_says_so(js):
    results = [
        {"device_id": "dA", "device_name": "machine 2", "ok": True, "gflops": 65.0,
         "bandwidth_mb_s": 90.0, "latency_ms": 0.4, "sources": {}, "warnings": []},
        {"device_id": "dB", "device_name": "machine 3", "ok": True, "gflops": 63.0,
         "bandwidth_mb_s": None, "latency_ms": 0.4, "sources": {}, "warnings": []},
    ]
    run(js, {"results": results, "summary": {}})
    n = json.loads(js.eval("JSON.stringify(Component.prototype.siNoticeFor(LAST_NOTICE, false))"))
    assert "MB/s (1/2)" in n["noticeText"]


# ------------------------------------------------------- the fleet-wide pass
TWO_STAGES = [
    {"id": "s1", "name": "Edge", "devices": [
        {"id": "dA", "name": "machine 2"}, {"id": "dB", "name": "machine 3"}]},
    {"id": "s2", "name": "Cloud", "devices": [{"id": "dC", "name": "device-1"}]},
]


def fleet(js, response, busy=False):
    js.eval(f"""
      var _self = Object.assign(Object.create(Component.prototype), {{
        state: {{ stages: {json.dumps(TWO_STAGES)},
                 siMeasuring: {json.dumps({'s1': True} if busy else {})},
                 siMeasured: {{}}, ssh: {{ out: [], outBy: {{}} }} }},
        setState: function (patch) {{
          var next = typeof patch === 'function' ? patch(this.state) : patch;
          for (var k in next) this.state[k] = next[k];
        }},
        sshLog: function (lines) {{
          lines.forEach(function (l) {{ CALLS.log.push({{ text: l.text, device: null }}); }});
        }},
        updateDevice: function (stageId, id, patch) {{
          CALLS.patches.push({{ stage: stageId, id: id, patch: patch }});
        }},
        siSyncInventory: function () {{ CALLS.synced = true; return Promise.resolve(); }}
      }});
      CALLS.patches = []; CALLS.log = []; LAST = null;
      window.SplitInference.api.measureFleet = function (body) {{
        LAST = body; return Promise.resolve({json.dumps(response)});
      }};
      _self.siMeasureAll();
    """)
    for _ in range(200):
        if not js.execute_pending_job():
            break
    return json.loads(js.eval(
        "JSON.stringify({calls: CALLS, request: LAST, "
        "notices: _self.state.siMeasured, busy: _self.state.siMeasuring})"
    ))


FLEET_RESPONSE = {
    "results": [
        {"device_id": "dA", "device_name": "machine 2", "ok": True, "gflops": 65.0,
         "bandwidth_mb_s": 3.9, "latency_ms": 0.4, "sources": {}, "warnings": []},
        {"device_id": "dB", "device_name": "machine 3", "ok": True, "gflops": 63.0,
         "bandwidth_mb_s": 3.8, "latency_ms": 0.4, "sources": {}, "warnings": []},
        {"device_id": "dC", "device_name": "device-1", "ok": True, "gflops": 120.0,
         "bandwidth_mb_s": 3.9, "latency_ms": 0.3, "sources": {}, "warnings": []},
    ],
    "summary": {"devices": 3, "measured": 3, "contention_pass": True},
}


def test_measure_all_sends_every_device_in_one_request(js):
    """One request, not one per stage.

    Per-stage passes have the edges contending only with edges; a run has
    everything publishing into the same broker together, and only a single
    request reproduces that.
    """
    out = fleet(js, FLEET_RESPONSE)
    assert out["request"] == {
        "device_ids": ["dA", "dB", "dC"], "apply": True, "contention": True
    }
    assert out["calls"]["synced"] is True


def test_measure_all_writes_each_device_to_its_own_stage(js):
    out = fleet(js, FLEET_RESPONSE)
    by_id = {p["id"]: p["stage"] for p in out["calls"]["patches"]}
    assert by_id == {"dA": "s1", "dB": "s1", "dC": "s2"}


def test_measure_all_puts_a_notice_on_every_stage_it_touched(js):
    out = fleet(js, FLEET_RESPONSE)
    assert set(out["notices"]) == {"s1", "s2"}
    assert out["notices"]["s1"]["total"] == 2
    assert out["notices"]["s1"]["ok"] == ["machine 2", "machine 3"]
    assert out["notices"]["s2"]["total"] == 1
    assert out["notices"]["s2"]["ok"] == ["device-1"]
    assert out["busy"] == {}, "stages were left spinning"


def test_measure_all_is_ignored_while_anything_is_already_measuring(js):
    out = fleet(js, FLEET_RESPONSE, busy=True)
    assert out["request"] is None
    assert out["calls"]["patches"] == []


def test_a_result_for_a_device_no_stage_claims_is_dropped(js):
    """The inventory can drift between the request and the response."""
    response = {
        "results": FLEET_RESPONSE["results"] + [
            {"device_id": "ghost", "device_name": "removed", "ok": True,
             "gflops": 1.0, "sources": {}, "warnings": []}
        ],
        "summary": {},
    }
    out = fleet(js, response)
    assert "ghost" not in {p["id"] for p in out["calls"]["patches"]}
    assert set(out["notices"]) == {"s1", "s2"}


# ------------------------------------------------- Ctrl/Cmd+Enter for ▶ Run
#: The state the button is enabled in: something typed, something ticked,
#: nothing in flight.
READY = {"command": "nvidia-smi", "selected": ["dA"], "busy": False}


def press(js, event: dict | None = None, **ssh) -> dict:
    """One keystroke at the command box, with `ssh` state overrides applied."""
    state = {**READY, **ssh}
    return json.loads(
        js.eval(f"runKey({json.dumps(state)}, {json.dumps(event or {})})")
    )


def test_ctrl_enter_in_the_command_box_runs_it(js):
    out = press(js)
    assert out["ran"] == 1
    assert out["prevented"] == 1, "the keystroke was left to the browser as well"


def test_cmd_enter_does_the_same_on_a_mac(js):
    assert press(js, {"ctrlKey": False, "metaKey": True})["ran"] == 1


def test_enter_on_its_own_is_not_the_shortcut(js):
    """A one-line input swallows Enter; running on it would make every typo a
    fan-out to the whole selection."""
    out = press(js, {"ctrlKey": False})
    assert out["ran"] == 0
    assert out["prevented"] == 0


def test_another_field_on_the_tab_keeps_its_keystroke(js):
    """The Control tab is full of inputs -- the scp path, the server login --
    and none of them mean "run the command"."""
    other = {"target": {"tagName": "INPUT", "value": "/home/dai/results"}}
    assert press(js, other)["ran"] == 0
    # The preset editor is a textarea holding the whole list, not one command.
    editor = {"target": {"tagName": "TEXTAREA", "value": "gpu = nvidia-smi"}}
    assert press(js, editor)["ran"] == 0


def test_the_shortcut_refuses_whatever_the_button_refuses(js):
    """`runDisabled` on the button is no targets or a call already in flight;
    a keystroke that ignored it would start a second run."""
    assert press(js, selected=[])["ran"] == 0
    assert press(js, busy=True)["ran"] == 0
    # An empty box with a stale selection is the same click that does nothing.
    assert press(js, {"target": {"tagName": "INPUT", "value": "   "}},
                 command="   ")["ran"] == 0


def test_a_modified_ctrl_enter_is_left_alone(js):
    """Ctrl+Shift+Enter and Ctrl+Alt+Enter belong to whoever bound them."""
    assert press(js, {"shiftKey": True})["ran"] == 0
    assert press(js, {"altKey": True})["ran"] == 0
