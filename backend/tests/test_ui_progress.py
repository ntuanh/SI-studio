"""The Progress tab's handlers, driven in a real JavaScript engine.

Same reasoning as `test_ui_measure.py`: this code only ever runs in a browser,
so it is the part that rots silently. A renamed frame field here does not throw
— it shows up as a status board that quietly stops advancing while the run
itself is fine, which is the worst failure mode a progress display can have.

Covered: the stream frames that drive the board (including that a high-frequency
`autorun_progress` cannot clobber a step's verdict), and the render values the
markup binds to — the run/stop button's enabled states, the metric strip that
answers "how far in is it", and the bar refusing to invent a denominator.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT / "tools"))

quickjs = pytest.importorskip("quickjs", reason="pip install quickjs to run the UI tests")

LIVE_PATCH = BACKEND_ROOT / "ui" / "live-patch.js"

PRELUDE = """
var console = { warn: function () {}, log: function () {}, error: function () {} };
function Component() {}
Component.prototype = {
  componentDidMount: function () {}, componentDidUpdate: function () {},
  componentWillUnmount: function () {}, renderVals: function () { return {}; },
  simCluster: function () { return null; }, runSim: function () {},
  runFlow: function () {}, sshConnectAll: function () {}, sshRun: function () {},
  sshScp: function () {}, sshServerTest: function () {},
  removeStage: function () {}, removeDevice: function () {}
};
var STARTED = [];
var STOPPED = 0;
var window = {
  confirm: function () { return true; },
  SplitInference: {
    config: { baseUrl: 'http://localhost:8000' },
    isLive: function () { return true; },
    configure: function () {},
    api: {
      autorunStart: function (script, opts) {
        STARTED.push({ script: script, opts: opts });
        return Promise.resolve({ run: { id: 'r1', running: true, steps: [] },
                                 notify: { enabled: true } });
      },
      autorunStop: function () { STOPPED++; return Promise.resolve({ stopped: true }); },
      autorunStatus: function () { return Promise.resolve({ active: null, notify: {} }); },
      autorunScripts: function () { return Promise.resolve({ scripts: [] }); }
    }
  },
  addEventListener: function () {},
  localStorage: { getItem: function () { return null; }, setItem: function () {} },
  location: { origin: 'http://localhost:8000' }
};
var document = { addEventListener: function () {},
                 createElement: function () { return { style: {}, appendChild: function () {} }; } };
var URL = { revokeObjectURL: function () {} };
"""

HARNESS = """
function makeSelf(prog) {
  return Object.assign(Object.create(Component.prototype), {
    state: { active: 'progress', prog: prog || {}, ssh: { out: [], outBy: {} } },
    setState: function (patch) {
      var next = typeof patch === 'function' ? patch(this.state) : patch;
      for (var k in next) this.state[k] = next[k];
    },
    sshLog: function () {},
    siOpenStream: function () {}
  });
}

/* Feed frames in order, return the resulting run object. */
function feed(prog, frames) {
  var self = makeSelf(prog);
  frames.forEach(function (f) { Component.prototype.progOnEvent.call(self, f); });
  return JSON.stringify({ run: self.state.prog.run, log: self.state.prog.log || [] });
}

function vals(prog) {
  var self = makeSelf(prog);
  var R = Component.prototype.progRenderVals.call(self, self.state);
  // Functions do not survive JSON; the tests assert on the values.
  return JSON.stringify(R, function (k, v) {
    return typeof v === 'function' ? '[fn]' : v;
  });
}

function pressRun(prog) {
  STARTED = [];
  var self = makeSelf(prog);
  Component.prototype.progRun.call(self);
  return JSON.stringify({ started: STARTED, log: self.state.prog.log || [] });
}
"""


@pytest.fixture(scope="module")
def js():
    ctx = quickjs.Context()
    ctx.eval(PRELUDE)
    ctx.eval(LIVE_PATCH.read_text(encoding="utf-8"))
    ctx.eval(HARNESS)
    assert ctx.eval("typeof Component.prototype.progRenderVals") == "function"
    assert ctx.eval("typeof Component.prototype.progOnEvent") == "function"
    return ctx


def feed(js, prog: dict, frames: list[dict]) -> dict:
    return json.loads(js.eval(f"feed({json.dumps(prog)}, {json.dumps(frames)})"))


def vals(js, prog: dict) -> dict:
    return json.loads(js.eval(f"vals({json.dumps(prog)})"))


RUN = {
    "id": "260815-2041-fleet",
    "running": True,
    "status": "running",
    "duration_s": 130,
    "counts": {"total": 3, "ok": 0, "failed": 0},
    "current_step": "split",
    "steps": [{"index": 1, "name": "split", "status": "running", "duration_s": 130,
               "rc": None, "progress": {}, "progress_text": ""}],
}


# ------------------------------------------------------------------- frames
def test_started_and_finished_frames_replace_the_run(js):
    out = feed(js, {}, [{"name": "autorun_started", "run": RUN}])
    assert out["run"]["id"] == RUN["id"]

    done = dict(RUN, running=False, status="ok", exit_code=0,
                counts={"total": 3, "ok": 3, "failed": 0})
    out = feed(js, {"run": RUN}, [{"name": "autorun_finished", "run": done}])
    assert out["run"]["status"] == "ok"
    assert any("3/3 ok" in l["text"] for l in out["log"])


def test_progress_frame_updates_counters_of_the_open_step(js):
    out = feed(js, {"run": RUN}, [{
        "name": "autorun_progress", "step": 1, "step_name": "split",
        "progress": {"batch": "128", "total": "261", "fps": "16.53"}, "text": "",
    }])
    assert out["run"]["steps"][0]["progress"]["batch"] == "128"


def test_progress_frame_cannot_overwrite_a_verdict(js):
    """The high-frequency frame carries no status/rc. If it replaced the step
    wholesale, every poll would resurrect a finished project as 'running'."""
    finished = dict(RUN, steps=[dict(RUN["steps"][0], status="failed", rc=1)])
    out = feed(js, {"run": finished}, [{
        "name": "autorun_progress", "step": 1, "step_name": "split",
        "progress": {"batch": "9"}, "text": "",
    }])
    step = out["run"]["steps"][0]
    assert step["status"] == "failed" and step["rc"] == 1
    assert step["progress"]["batch"] == "9"


def test_progress_for_an_unseen_step_is_dropped(js):
    """Frames can arrive out of order; inventing a step from counters alone
    would put a nameless row on the board."""
    out = feed(js, {"run": RUN}, [{
        "name": "autorun_progress", "step": 7, "step_name": "dmsf",
        "progress": {"batch": "1"}, "text": "",
    }])
    assert len(out["run"]["steps"]) == 1


def test_step_frame_appends_a_new_project(js):
    out = feed(js, {"run": RUN}, [{
        "name": "autorun_step",
        "step": {"index": 2, "name": "PA", "status": "running", "duration_s": 0,
                 "rc": None, "progress": {}, "progress_text": ""},
    }])
    assert [s["name"] for s in out["run"]["steps"]] == ["split", "PA"]


def test_note_and_stall_frames_reach_the_transcript(js):
    out = feed(js, {"run": RUN}, [
        {"name": "autorun_note", "text": "[PA] 18 clients launched"},
        {"name": "autorun_stalled", "quiet_s": 912.4},
    ])
    text = "\n".join(l["text"] for l in out["log"])
    assert "18 clients launched" in text
    assert "912" in text and "still running" in text


def test_unrelated_events_are_not_claimed(js):
    """`progOnEvent` returning false is what lets the console keep narrating
    deploy/run events it still owns."""
    assert js.eval(
        "String(Component.prototype.progOnEvent.call(makeSelf({}), "
        "{ name: 'deploy_started' }))"
    ) == "false"


# ------------------------------------------------------------ render values
def test_idle_board_invites_a_run(js):
    R = vals(js, {"scripts": [{"name": "fleet-3project.sh"}], "script": "fleet-3project.sh"})
    assert R["headline"] == "Idle"
    assert R["runDisabled"] is False
    assert R["stopDisabled"] is True
    assert "Nothing has run yet" in str(R["emptyStyle"]) or R["emptyStyle"] != "display:none;"


def test_running_disables_run_and_enables_stop(js):
    R = vals(js, {"run": RUN, "script": "fleet-3project.sh"})
    assert R["runDisabled"] is True
    assert R["stopDisabled"] is False
    assert R["headline"] == "split"


def test_metric_strip_leads_with_batch_then_fps(js):
    """Batch first because that is what moves; fps second because that is what
    the run is for."""
    run = dict(RUN, steps=[dict(RUN["steps"][0], progress={
        "batch": "128", "total": "261", "fps": "16.53", "reg": "18/18"})])
    R = vals(js, {"run": run})
    metrics = R["steps"][0]["metrics"]
    assert metrics.startswith("batch 128")
    assert "16.53 fps" in metrics and "reg 18/18" in metrics


def test_bar_needs_a_real_total(js):
    """Without `total` the script never said how long the project is, and a
    bar drawn against a guessed denominator is a lie."""
    no_total = dict(RUN, steps=[dict(RUN["steps"][0], progress={"batch": "128"})])
    assert vals(js, {"run": no_total})["steps"][0]["barTrackStyle"] == "display:none;"

    with_total = dict(RUN, steps=[dict(RUN["steps"][0],
                                       progress={"batch": "130", "total": "260"})])
    row = vals(js, {"run": with_total})["steps"][0]
    assert row["barTrackStyle"] != "display:none;"
    assert "50.0%" in row["barFillStyle"]


def test_finished_projects_show_their_verdict(js):
    run = dict(RUN, running=False, status="failed",
               counts={"total": 2, "ok": 1, "failed": 1},
               steps=[
                   {"index": 1, "name": "split", "status": "ok", "rc": 0,
                    "duration_s": 724, "progress": {}, "progress_text": ""},
                   {"index": 2, "name": "PA", "status": "failed", "rc": 1,
                    "duration_s": 63, "progress": {}, "progress_text": ""},
               ])
    R = vals(js, {"run": run})
    assert R["steps"][0]["badge"] == "✔"
    assert R["steps"][1]["badge"] == "rc 1"
    assert R["steps"][0]["duration"] == "12m 04s"
    assert R["headline"] == "Failed"


def test_notification_state_is_stated_not_assumed(js):
    """Whether an unattended run can actually reach you is the one setting
    whose absence is silent until it matters."""
    off = vals(js, {"notify": {"enabled": False}})
    assert "off" in off["notifyLabel"]
    assert "TELEGRAM_BOT_TOKEN" in off["notifyTitle"]

    on = vals(js, {"notify": {"enabled": True, "chat_id": "123456789"}})
    assert "on" in on["notifyLabel"]
    assert "123456789" in on["notifyTitle"]


def test_run_button_asks_for_notifications(js):
    out = json.loads(js.eval('pressRun({ script: "fleet-3project.sh" })'))
    assert out["started"][0]["script"] == "fleet-3project.sh"
    assert out["started"][0]["opts"]["notify"] is True


def test_run_without_a_script_does_not_call_the_api(js):
    out = json.loads(js.eval('pressRun({ script: "" })'))
    assert out["started"] == []
    assert any("pick a schedule" in l["text"] for l in out["log"])
