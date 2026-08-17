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
var QUEUE_STARTED = [];
var QUEUE_STOPPED = 0;
var SAVED = [];
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
      autorunScripts: function () { return Promise.resolve({ scripts: [] }); },
      queueStart: function (opts) {
        QUEUE_STARTED.push(opts || {});
        return Promise.resolve({ run: { id: 'q1', running: true, steps: [] },
                                 notify: { enabled: true } });
      },
      queueStop: function () { QUEUE_STOPPED++; return Promise.resolve({ stopped: true, jobs: 4 }); },
      queueStatus: function () { return Promise.resolve({ active: null, notify: {}, targets: [] }); },
      queueProjects: function () { return Promise.resolve({ projects: [] }); },
      saveQueueProjects: function (projects) {
        SAVED.push(projects);
        return Promise.resolve({ projects: projects });
      }
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

function pressRunAll(prog) {
  QUEUE_STARTED = [];
  var self = makeSelf(prog);
  Component.prototype.progRunAll.call(self);
  return JSON.stringify({
    started: QUEUE_STARTED, log: self.state.prog.log || [],
    edit: self.state.prog.edit
  });
}

function pressStop(prog) {
  STOPPED = 0; QUEUE_STOPPED = 0;
  var self = makeSelf(prog);
  Component.prototype.progStop.call(self);
  return JSON.stringify({ script: STOPPED, queue: QUEUE_STOPPED });
}

/* Drive the editor: open it, apply a list of [method, ...args] calls, and
   report both the draft and whatever reached the API. */
function editor(prog, steps, dirs) {
  SAVED = [];
  var self = makeSelf(prog);
  self.state.siDirs = dirs || [];
  Component.prototype.progEditToggle.call(self, true);
  steps.forEach(function (s) {
    Component.prototype[s[0]].apply(self, s.slice(1));
  });
  return JSON.stringify({
    draft: self.state.prog.edit,
    saved: SAVED,
    err: self.state.prog.editErr || '',
    vals: JSON.parse(JSON.stringify(
      Component.prototype.progEditVals.call(self, self.state.prog),
      function (k, v) { return typeof v === 'function' ? '[fn]' : v; }
    ))
  });
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


# =========================================================== the project queue
PROJECTS = [
    {"name": "split", "path": "ntuanh/Optimizer/split_inference_test",
     "enabled": True, "expected_s": 720},
    {"name": "PA", "path": "ntuanh/split_inference_test", "enabled": True, "expected_s": 540},
    {"name": "dmsf", "path": "manh224353/split_inference", "enabled": False, "expected_s": 0},
]

QUEUE_RUN = {
    "id": "q1a2b3c4",
    "running": True,
    "status": "running",
    "duration_s": 90,
    "expected_steps": 3,
    "counts": {"total": 3, "ok": 1, "failed": 0, "running": 1, "stopped": 0},
    "current_step": "PA",
    "steps": [
        {"index": 1, "name": "split", "status": "ok", "rc": 0, "duration_s": 712,
         "progress": {}, "progress_text": ""},
        {"index": 2, "name": "PA", "status": "running", "rc": None, "duration_s": 90,
         "progress": {"phase": "3", "phases": "3", "elapsed_s": "90", "expected_s": "540"},
         "progress_text": "running"},
        {"index": 3, "name": "dmsf", "status": "queued", "rc": None, "duration_s": 0,
         "progress": {"phase": "0", "phases": "3"}, "progress_text": "manh224353/split_inference"},
    ],
}


def test_the_button_counts_the_projects_it_will_run(js):
    """One click, and it says up front what it is about to do — the disabled
    projects are not in the count because they are not in the run."""
    R = vals(js, {"projects": PROJECTS})
    assert R["runAllLabel"] == "▶ Run all 2 projects"
    assert R["runAllDisabled"] is False
    assert "split → PA" in R["runAllTitle"]


def test_run_all_starts_the_queue_with_notifications(js):
    out = json.loads(js.eval(f"pressRunAll({json.dumps({'projects': PROJECTS})})"))
    assert out["started"] == [{"notify": True, "notifySteps": True}]
    assert any("split → PA" in l["text"] for l in out["log"])


def test_run_all_with_no_projects_opens_the_editor_instead_of_failing(js):
    """They have not filled it in yet; the fix is the panel, not a red line."""
    out = json.loads(js.eval("pressRunAll({ projects: [] })"))
    assert out["started"] == []
    assert out["edit"] == []
    assert any("no projects yet" in l["text"] for l in out["log"])


def test_stop_goes_to_whichever_launcher_is_running(js):
    """The board does not distinguish the two, so neither should the button."""
    q = json.loads(js.eval(f"pressStop({json.dumps({'run': QUEUE_RUN, 'source': 'queue'})})"))
    assert (q["queue"], q["script"]) == (1, 0)

    s = json.loads(js.eval(f"pressStop({json.dumps({'run': RUN, 'source': 'script'})})"))
    assert (s["queue"], s["script"]) == (0, 1)


# ------------------------------------------------------------- the queue bar
def test_the_queue_bar_counts_settled_projects_against_the_plan(js):
    """The plan is known before the first project starts, so this denominator
    is a fact. One finished plus one open = 1.5 of 3."""
    R = vals(js, {"run": QUEUE_RUN})
    assert R["queueLabel"] == "1 of 3 done"
    assert R["queuePct"] == pytest.approx(50.0)
    assert R["queueStyle"] != "display:none;"


def test_the_queue_bar_is_hidden_when_nothing_has_run(js):
    assert vals(js, {"projects": PROJECTS})["queueStyle"] == "display:none;"


# ------------------------------------------------- the per-project bar sources
def test_the_bar_prefers_a_measured_denominator(js):
    """batch/total is read out of the run's own output — it beats both of the
    weaker sources whenever it is there."""
    run = dict(QUEUE_RUN, steps=[dict(QUEUE_RUN["steps"][1], progress={
        "batch": "128", "total": "256", "elapsed_s": "500", "expected_s": "540",
        "phase": "3", "phases": "3"})])
    row = vals(js, {"run": run})["steps"][0]
    assert "50.0%" in row["barFillStyle"]
    assert row["barTitle"].startswith("measured:")


def test_the_bar_falls_back_to_the_operators_estimate(js):
    """No batch counter, but the editor supplied an expected duration."""
    row = vals(js, {"run": QUEUE_RUN})["steps"][1]
    assert row["barTitle"].startswith("estimated:")
    assert "16.7%" in row["barFillStyle"]


def test_an_overrunning_estimate_never_claims_the_run_is_over(js):
    """Overrunning the estimate is the most common thing for it to do, and a
    full bar on a still-running project reads as finished."""
    run = dict(QUEUE_RUN, steps=[dict(QUEUE_RUN["steps"][1], progress={
        "elapsed_s": "5400", "expected_s": "540"})])
    row = vals(js, {"run": run})["steps"][0]
    assert "97.0%" in row["barFillStyle"]


def test_the_bar_falls_back_to_the_launch_phase(js):
    """Structural, always available: server up, then each stage launched. It is
    what a project with no counters and no estimate still honestly has."""
    run = dict(QUEUE_RUN, steps=[dict(QUEUE_RUN["steps"][1],
                                      progress={"phase": "2", "phases": "3"})])
    row = vals(js, {"run": run})["steps"][0]
    assert row["barTitle"].startswith("launching:")
    assert "66.7%" in row["barFillStyle"]


def test_with_no_source_at_all_there_is_still_no_bar(js):
    """The rule the schedule scripts have always followed survives the rework:
    a bar with a made-up denominator is worse than no bar."""
    run = dict(QUEUE_RUN, steps=[dict(QUEUE_RUN["steps"][1], progress={})])
    assert vals(js, {"run": run})["steps"][0]["barTrackStyle"] == "display:none;"


def test_a_queued_project_is_on_the_board_but_reports_no_runtime(js):
    """The whole plan is visible from the start; a row that has not begun must
    not show a duration or a bar."""
    row = vals(js, {"run": QUEUE_RUN})["steps"][2]
    assert row["badge"] == "queued"
    assert row["duration"] == ""
    assert row["barTrackStyle"] == "display:none;"


# ------------------------------------------------------------- the editor
def _edit(js, prog, steps, dirs=None):
    return json.loads(js.eval(
        f"editor({json.dumps(prog)}, {json.dumps(steps)}, {json.dumps(dirs or [])})"
    ))


def test_opening_the_editor_drafts_the_saved_projects(js):
    out = _edit(js, {"projects": PROJECTS}, [])
    assert [r["path"] for r in out["draft"]] == [p["path"] for p in PROJECTS]
    # Seconds are shown as minutes: nobody thinks about a project in seconds.
    assert out["draft"][0]["expectedMin"] == "12"
    assert out["draft"][2]["enabled"] is False


def test_edits_do_not_reach_the_server_until_save(js):
    out = _edit(js, {"projects": PROJECTS}, [["progEditPatch", 0, {"path": "half-typed"}]])
    assert out["saved"] == []
    assert out["draft"][0]["path"] == "half-typed"


def test_saving_sends_the_list_in_editor_order(js):
    out = _edit(js, {"projects": PROJECTS}, [
        ["progEditMove", 2, -1],
        ["progEditSave"],
    ])
    assert [p["name"] for p in out["saved"][0]] == ["split", "dmsf", "PA"]


def test_minutes_are_converted_back_to_seconds(js):
    out = _edit(js, {"projects": PROJECTS}, [
        ["progEditPatch", 0, {"expectedMin": "9"}],
        ["progEditSave"],
    ])
    assert out["saved"][0][0]["expected_s"] == 540


def test_a_blank_estimate_stays_blank_rather_than_becoming_zero_minutes(js):
    out = _edit(js, {"projects": PROJECTS}, [
        ["progEditPatch", 0, {"expectedMin": ""}],
        ["progEditSave"],
    ])
    assert out["saved"][0][0]["expected_s"] == 0


def test_adding_and_removing_rows(js):
    out = _edit(js, {"projects": PROJECTS}, [
        ["progEditRemove", 1],
        ["progEditAdd", "new/project", "new"],
        ["progEditSave"],
    ])
    assert [p["name"] for p in out["saved"][0]] == ["split", "dmsf", "new"]


def test_a_row_with_no_directory_is_dropped_rather_than_saved(js):
    out = _edit(js, {"projects": PROJECTS}, [["progEditAdd"], ["progEditSave"]])
    assert len(out["saved"][0]) == 3


def test_two_projects_on_one_directory_is_refused(js):
    """The second would run in a directory the first has just written its
    results into."""
    out = _edit(js, {"projects": PROJECTS}, [
        ["progEditPatch", 1, {"path": PROJECTS[0]["path"]}],
        ["progEditSave"],
    ])
    assert out["saved"] == []
    assert "share the directory" in out["err"]


def test_control_directories_can_be_imported(js):
    """They are almost always the projects, and a path one tab away cannot be
    retyped wrong if it is not retyped."""
    dirs = [{"label": "split_inference_test", "path": "ntuanh/Optimizer/split_inference_test"},
            {"label": "SplittingYOLO", "path": "ntuanh/SplittingYOLO"}]
    out = _edit(js, {"projects": []}, [["progEditImport"], ["progEditSave"]], dirs)
    assert [p["name"] for p in out["saved"][0]] == ["split_inference_test", "SplittingYOLO"]


def test_importing_skips_directories_already_in_the_list(js):
    """Pressing it twice must not double the queue."""
    dirs = [{"label": "split_inference_test", "path": "ntuanh/Optimizer/split_inference_test"}]
    out = _edit(js, {"projects": PROJECTS}, [["progEditImport"]], dirs)
    assert len(out["draft"]) == len(PROJECTS)
    assert out["err"] == "every saved directory is already listed"


def test_the_editor_shows_what_will_actually_run(js):
    """The commands live on the Control tab, so without this the editor looks
    like it is missing half its fields."""
    out = _edit(js, {"projects": PROJECTS, "targets": [
        {"key": "__server__", "label": "Control server",
         "command": "python3 server.py", "devices": ["__server__"]},
        {"key": "sA", "label": "Edge", "command": "python3 client.py --layer_id 1",
         "devices": ["d1", "d2"]},
    ]}, [])
    plan = out["vals"]["plan"]
    assert [p["label"] for p in plan] == ["Control server", "Edge"]
    assert plan[1]["hosts"] == "2 host(s)"
