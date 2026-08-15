"""Auto-run: script sandbox, marker parsing, lifecycle, and notification text.

Hermetic — the only thing actually executed is a throwaway bash script written
into the test's own sandbox, and the notifier's HTTP transport is always stubbed.
Tests that need a real shell skip when bash is absent, so the suite still passes
on a Windows box without Git Bash.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from app.config import settings
from app.services import autorun as ar
from app.services.autorun import AutoRun, AutoRunError, AutoRunner, Step, _banner_name

HAS_BASH = ar._find_bash() is not None
needs_bash = pytest.mark.skipif(not HAS_BASH, reason="no bash on PATH")


@pytest.fixture()
def sandbox(tmp_path, monkeypatch) -> Path:
    """A fresh AUTORUN_DIR per test, with a runner pointed at it."""
    monkeypatch.setattr(settings, "autorun_dir", str(tmp_path))
    monkeypatch.setattr(settings, "autorun_allow_any_path", False)
    return tmp_path


@pytest.fixture()
def runner(sandbox) -> AutoRunner:
    return AutoRunner()


def write_script(sandbox: Path, name: str, body: str) -> Path:
    path = sandbox / name
    path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


# --------------------------------------------------------------- path sandbox
def test_script_resolves_relative_to_autorun_dir(runner, sandbox):
    write_script(sandbox, "ok.sh", "echo hi\n")
    assert runner.resolve_script("ok.sh") == (sandbox / "ok.sh").resolve()


def test_script_outside_sandbox_is_refused(runner, sandbox, tmp_path_factory):
    outside = tmp_path_factory.mktemp("elsewhere") / "evil.sh"
    outside.write_text("echo pwned\n", encoding="utf-8")
    with pytest.raises(AutoRunError, match="outside AUTORUN_DIR"):
        runner.resolve_script(str(outside))


def test_dotdot_traversal_is_refused(runner, sandbox):
    with pytest.raises(AutoRunError, match="outside AUTORUN_DIR"):
        runner.resolve_script("../../etc/passwd")


def test_missing_script_is_refused(runner, sandbox):
    with pytest.raises(AutoRunError, match="no such script"):
        runner.resolve_script("nope.sh")


def test_directory_is_not_a_script(runner, sandbox):
    (sandbox / "adir").mkdir()
    with pytest.raises(AutoRunError, match="not a file"):
        runner.resolve_script("adir")


def test_allow_any_path_lifts_the_sandbox(runner, sandbox, tmp_path_factory, monkeypatch):
    outside = tmp_path_factory.mktemp("elsewhere") / "fine.sh"
    outside.write_text("echo ok\n", encoding="utf-8")
    monkeypatch.setattr(settings, "autorun_allow_any_path", True)
    assert runner.resolve_script(str(outside)) == outside.resolve()


def test_list_scripts_finds_shell_files_only(runner, sandbox):
    write_script(sandbox, "a.sh", "true\n")
    write_script(sandbox, "b.bash", "true\n")
    (sandbox / "notes.txt").write_text("ignore me", encoding="utf-8")
    names = {s["name"] for s in runner.list_scripts()}
    assert names == {"a.sh", "b.bash"}


def test_list_scripts_excludes_captured_run_output(runner, sandbox):
    """A transcript that happens to be named *.sh must not look runnable."""
    write_script(sandbox, "real.sh", "true\n")
    stray = sandbox / "runs" / "250101-000000-x"
    stray.mkdir(parents=True)
    (stray / "replay.sh").write_text("echo nope", encoding="utf-8")
    assert {s["name"] for s in runner.list_scripts()} == {"real.sh"}


# -------------------------------------------------------------- banner parsing
@pytest.mark.parametrize(
    "line,expected",
    [
        ("=== DAG ===", "DAG"),
        ("==== training dmsf ====", "training dmsf"),
        ("--- standalone", "standalone"),
        ("### Privacy Aware", "Privacy Aware"),
        (">>> step four", "step four"),
    ],
)
def test_banner_names_are_extracted(line, expected):
    assert _banner_name(line) == expected


@pytest.mark.parametrize(
    "line",
    [
        "========================",  # a rule, not a banner
        "------------------------",
        "-=-=-=-=-=-=-=-",
        "plain output line",
        "--- " + "x" * 200,  # prose, not a project name
        "",
    ],
)
def test_non_banners_are_ignored(line):
    assert _banner_name(line) is None


# ------------------------------------------------------------ marker tracking
async def feed(
    run: AutoRun,
    runner: AutoRunner,
    lines: list[str],
    markers: str = "auto",
    notify: bool = False,
) -> None:
    run.markers = markers
    heuristics = markers == "auto"
    for line in lines:
        heuristics = await runner._interpret(run, line, heuristics, notify=notify)


def make_run(runner: AutoRunner, markers: str = "auto") -> AutoRun:
    run = AutoRun(id="t-1", script=str(runner.root / "s.sh"), markers=markers)
    runner._run = run
    return run


@pytest.mark.asyncio
async def test_explicit_markers_track_steps_and_codes(runner):
    run = make_run(runner)
    await feed(
        run,
        runner,
        [
            "::step:: dmsf",
            "training…",
            "::step-done:: dmsf rc=0",
            "::step:: DAG",
            "::step-done:: DAG rc=1",
        ],
    )
    assert [(s.name, s.status, s.rc) for s in run.steps] == [
        ("dmsf", "ok", 0),
        ("DAG", "failed", 1),
    ]
    assert run.counts() == {"total": 2, "ok": 1, "failed": 1, "running": 0, "stopped": 0}


@pytest.mark.asyncio
async def test_counter_marker_sets_expected_total(runner):
    run = make_run(runner)
    await feed(run, runner, ["[1/4] dmsf", "[2/4] DAG"])
    assert run.expected_steps == 4
    assert [s.name for s in run.steps] == ["dmsf", "DAG"]
    # The first is closed implicitly when the second begins.
    assert run.steps[0].status == "ok"


@pytest.mark.asyncio
async def test_explicit_marker_disables_heuristics_permanently(runner):
    """A script printing both must be read once, not counted twice."""
    run = make_run(runner)
    await feed(
        run,
        runner,
        [
            "::step:: dmsf",
            "=== dmsf ===",       # the script's own banner for the same project
            "::step-done:: dmsf rc=0",
            "=== DAG ===",        # still ignored: heuristics are off for good
        ],
    )
    assert [s.name for s in run.steps] == ["dmsf"]


@pytest.mark.asyncio
async def test_strict_mode_ignores_banners(runner):
    run = make_run(runner, markers="strict")
    await feed(run, runner, ["=== DAG ===", "[1/3] x"], markers="strict")
    assert run.steps == []


@pytest.mark.asyncio
async def test_repeated_banner_does_not_open_a_second_step(runner):
    run = make_run(runner)
    await feed(run, runner, ["=== DAG ===", "output", "=== DAG ==="])
    assert len(run.steps) == 1


@pytest.mark.asyncio
async def test_fail_marker_fails_the_open_step(runner):
    run = make_run(runner)
    await feed(run, runner, ["::step:: PA", "::fail:: missing directory"])
    assert run.steps[0].status == "failed"
    assert run.steps[0].detail == "missing directory"


@pytest.mark.asyncio
async def test_step_done_without_an_open_step_is_still_recorded(runner):
    run = make_run(runner)
    await feed(run, runner, ["::step-done:: orphan rc=2"], markers="strict")
    assert [(s.name, s.status, s.rc) for s in run.steps] == [("orphan", "failed", 2)]


@pytest.mark.asyncio
async def test_step_done_without_rc_counts_as_success(runner):
    run = make_run(runner)
    await feed(run, runner, ["::step:: x", "::step-done:: x"])
    assert run.steps[0].status == "ok"


@pytest.mark.asyncio
async def test_counter_inside_an_explicit_marker_sets_the_total(runner):
    """`::step:: [2/4] DAG` carries the total just as a bare `[2/4]` line does,
    and the label keeps only the project name."""
    run = make_run(runner)
    await feed(run, runner, ["::step:: [1/4] dmsf", "::step-done:: dmsf rc=0"])
    assert run.expected_steps == 4
    assert run.steps[0].name == "dmsf"


@pytest.mark.asyncio
async def test_counter_prefix_is_not_stripped_into_an_empty_name(runner):
    run = make_run(runner)
    await feed(run, runner, ["::step:: [2/9]"])
    assert run.expected_steps == 9
    assert run.steps[0].name == "[2/9]"


# --------------------------------------------------------- env passthrough
def test_env_passthrough_forwards_only_prefixed_keys(tmp_path, monkeypatch):
    """pydantic-settings parses .env into the Settings object and nowhere else,
    so a schedule inherits nothing unless this forwards it."""
    envfile = tmp_path / ".env"
    envfile.write_text(
        "API_TOKEN=super-secret\n"
        "TELEGRAM_BOT_TOKEN=111:AAsecret\n"
        "FLEET_DAI_USER=dai\n"
        "FLEET_DAI_PASS=not-a-real-password\n"
        "# FLEET_COMMENTED=no\n"
        "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(ar, "BACKEND_ROOT", tmp_path)
    monkeypatch.setattr(settings, "autorun_env_prefixes", "FLEET_")

    out = ar.env_passthrough()
    assert out == {"FLEET_DAI_USER": "dai", "FLEET_DAI_PASS": "not-a-real-password"}


def test_env_passthrough_withholds_the_api_and_bot_tokens(tmp_path, monkeypatch):
    """A script that runs `set -x` or dumps `env` on failure prints straight
    into a transcript this service stores and forwards to chat."""
    (tmp_path / ".env").write_text(
        "API_TOKEN=super-secret\nTELEGRAM_BOT_TOKEN=111:AAsecret\nFLEET_X=1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(ar, "BACKEND_ROOT", tmp_path)
    monkeypatch.setattr(settings, "autorun_env_prefixes", "FLEET_")

    blob = repr(ar.env_passthrough())
    assert "super-secret" not in blob and "AAsecret" not in blob


def test_env_passthrough_strips_quotes(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text('FLEET_PASS="trailing.dot."\n', encoding="utf-8")
    monkeypatch.setattr(ar, "BACKEND_ROOT", tmp_path)
    monkeypatch.setattr(settings, "autorun_env_prefixes", "FLEET_")
    assert ar.env_passthrough()["FLEET_PASS"] == "trailing.dot."


def test_env_passthrough_is_empty_without_prefixes(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("FLEET_X=1\n", encoding="utf-8")
    monkeypatch.setattr(ar, "BACKEND_ROOT", tmp_path)
    monkeypatch.setattr(settings, "autorun_env_prefixes", "")
    assert ar.env_passthrough() == {}


def test_env_passthrough_survives_a_missing_env_file(tmp_path, monkeypatch):
    monkeypatch.setattr(ar, "BACKEND_ROOT", tmp_path / "nope")
    monkeypatch.setattr(settings, "autorun_env_prefixes", "FLEET_")
    assert ar.env_passthrough() == {}


@pytest.mark.asyncio
@needs_bash
async def test_started_script_receives_the_forwarded_env(runner, sandbox, tmp_path, monkeypatch):
    """The end-to-end version of the bug: the schedule could not reach the
    fleet because its credentials never left the Settings object."""
    (tmp_path / ".env").write_text("FLEET_CANARY=reached-the-script\n", encoding="utf-8")
    monkeypatch.setattr(ar, "BACKEND_ROOT", tmp_path)
    monkeypatch.setattr(settings, "autorun_env_prefixes", "FLEET_")

    write_script(sandbox, "envcheck.sh", 'echo "canary=${FLEET_CANARY:-UNSET}"\n')
    run = await runner.start("envcheck.sh", notify=False)
    await asyncio.wait_for(runner._task, timeout=30)
    assert "canary=reached-the-script" in Path(run.log_path).read_text(encoding="utf-8")


# ------------------------------------------------------------ live progress
@pytest.mark.asyncio
async def test_progress_marker_updates_the_open_step(runner):
    run = make_run(runner)
    await feed(
        run,
        runner,
        ["::step:: PA", "::progress:: project=PA batch=128 fps=16.53 elapsed=240s"],
    )
    step = run.steps[0]
    assert step.progress == {
        "project": "PA", "batch": "128", "fps": "16.53", "elapsed": "240s"
    }
    assert step.status == "running"  # counters must not close the step


@pytest.mark.asyncio
async def test_progress_keeps_unstructured_remainder_as_text(runner):
    run = make_run(runner)
    await feed(run, runner, ["::step:: x", "::progress:: batch=3 waiting for the drain"])
    assert run.steps[0].progress == {"batch": "3"}
    assert run.steps[0].progress_text == "waiting for the drain"


@pytest.mark.asyncio
async def test_later_progress_merges_rather_than_replaces(runner):
    """`fps` arriving without `batch` must not erase the batch counter."""
    run = make_run(runner)
    await feed(
        run, runner,
        ["::step:: x", "::progress:: batch=10 fps=1.0", "::progress:: fps=2.0"],
    )
    assert run.steps[0].progress == {"batch": "10", "fps": "2.0"}


@pytest.mark.asyncio
async def test_progress_never_notifies(runner, monkeypatch):
    """The whole point of a separate marker: 80 polls must not be 80 messages."""
    sent: list[str] = []
    monkeypatch.setattr(settings, "notify_enabled", True)
    monkeypatch.setattr(settings, "telegram_bot_token", "1:x")
    monkeypatch.setattr(settings, "telegram_chat_id", "9")
    monkeypatch.setattr(ar.notifier, "_post", lambda m, p: sent.append(p["text"]) or {"ok": True})

    run = make_run(runner)
    lines = ["::step:: PA"] + [f"::progress:: batch={i}" for i in range(40)]
    await feed(run, runner, lines, notify=True)
    assert sent == []


@pytest.mark.asyncio
async def test_progress_before_any_step_is_ignored(runner):
    run = make_run(runner)
    await feed(run, runner, ["::progress:: batch=5"], markers="strict")
    assert run.steps == []


@pytest.mark.asyncio
async def test_progress_lines_are_not_quoted_into_alerts(runner):
    run = make_run(runner)
    step = Step(index=1, name="PA", status="failed", rc=1, finished_at=time.time())
    run.steps.append(step)
    runner._tail.extend(["::progress:: batch=128 fps=16.5", "RuntimeError: broker gone"])
    msg = runner._failure_message(run, step)
    assert "RuntimeError: broker gone" in msg and "::progress" not in msg


# ------------------------------------------------------- notification excerpts
def test_markers_are_not_quoted_back_as_output(runner):
    """The traceback is what matters; the protocol lines are noise."""
    run = make_run(runner)
    step = Step(index=1, name="DAG", status="failed", rc=1, finished_at=time.time())
    run.steps.append(step)
    runner._tail.extend(
        [
            "::note:: schedule starting",
            "::step:: [2/4] DAG",
            "loading config.yaml",
            "ValueError: bad shard",
            "::step-done:: DAG rc=1",
        ]
    )
    msg = runner._failure_message(run, step)
    assert "ValueError: bad shard" in msg and "loading config.yaml" in msg
    assert "::step" not in msg and "::note" not in msg


def test_summary_does_not_quote_output_from_after_the_failure(runner):
    """A mid-schedule failure leaves the transcript ending on a *success*.
    Quoting that under a FAILED headline points at the wrong project."""
    run = make_run(runner)
    run.status, run.exit_code, run.finished_at = "failed", 1, time.time()
    run.steps = [
        Step(index=1, name="DAG", status="failed", rc=1, finished_at=time.time()),
        Step(index=2, name="PA", status="ok", rc=0, finished_at=time.time()),
    ]
    runner._tail.extend(["loading config for PA", "PA finished cleanly"])

    msg = runner._summary_message(run)
    assert "failed: DAG" in msg
    assert "PA finished cleanly" not in msg


@pytest.mark.asyncio
async def test_step_message_numbers_by_position_not_by_successes(runner, monkeypatch):
    """After a failure the success count and the position diverge; the message
    must report the position."""
    sent: list[str] = []
    monkeypatch.setattr(settings, "notify_enabled", True)
    monkeypatch.setattr(settings, "telegram_bot_token", "1:x")
    monkeypatch.setattr(settings, "telegram_chat_id", "9")
    monkeypatch.setattr(ar.notifier, "_post", lambda m, p: sent.append(p["text"]) or {"ok": True})

    run = make_run(runner)
    runner._notify_steps = True
    await feed(
        run,
        runner,
        [
            "::step:: [1/4] a", "::step-done:: a rc=0",
            "::step:: [2/4] b", "::step-done:: b rc=1",   # failure
            "::step:: [3/4] c", "::step-done:: c rc=0",
        ],
        notify=True,
    )
    # `c` is the third project, whatever happened to `b`.
    assert any("step 3/4" in m for m in sent), sent


def test_summary_falls_back_to_the_tail_when_no_step_failed(runner):
    """Script died without closing a step — the transcript is all we have."""
    run = make_run(runner)
    run.status, run.exit_code, run.finished_at = "failed", 127, time.time()
    runner._tail.extend(["bash: python: command not found"])

    msg = runner._summary_message(run)
    assert "command not found" in msg


# ------------------------------------------------------------------ lifecycle
@pytest.mark.asyncio
@needs_bash
async def test_successful_run_reports_ok_and_saves_a_manifest(runner, sandbox):
    write_script(
        sandbox,
        "good.sh",
        'echo "::step:: alpha"\necho work\necho "::step-done:: alpha rc=0"\nexit 0\n',
    )
    run = await runner.start("good.sh", notify=False)
    await asyncio.wait_for(runner._task, timeout=30)

    assert run.status == "ok"
    assert run.exit_code == 0
    assert [(s.name, s.status) for s in run.steps] == [("alpha", "ok")]

    saved = runner.history()
    assert saved and saved[0]["id"] == run.id and saved[0]["status"] == "ok"
    assert "work" in Path(run.log_path).read_text(encoding="utf-8")


@pytest.mark.asyncio
@needs_bash
async def test_failing_script_is_reported_as_failed(runner, sandbox):
    write_script(sandbox, "bad.sh", "echo nope\nexit 3\n")
    run = await runner.start("bad.sh", notify=False)
    await asyncio.wait_for(runner._task, timeout=30)
    assert run.status == "failed"
    assert run.exit_code == 3


@pytest.mark.asyncio
@needs_bash
async def test_exit_code_is_authoritative_without_any_markers(runner, sandbox):
    """The whole-run verdict must not depend on the script cooperating."""
    write_script(sandbox, "silent.sh", "echo just output\nexit 1\n")
    run = await runner.start("silent.sh", markers="off", notify=False)
    await asyncio.wait_for(runner._task, timeout=30)
    assert run.steps == []
    assert run.status == "failed" and run.exit_code == 1


@pytest.mark.asyncio
@needs_bash
async def test_open_step_is_failed_when_the_script_dies(runner, sandbox):
    write_script(sandbox, "mid.sh", 'echo "::step:: alpha"\nexit 9\n')
    run = await runner.start("mid.sh", notify=False)
    await asyncio.wait_for(runner._task, timeout=30)
    assert run.steps[0].status == "failed"
    assert run.steps[0].rc == 9


@pytest.mark.asyncio
@needs_bash
async def test_second_start_is_rejected_while_one_runs(runner, sandbox):
    write_script(sandbox, "slow.sh", "sleep 20\n")
    write_script(sandbox, "other.sh", "true\n")
    await runner.start("slow.sh", notify=False)
    try:
        with pytest.raises(AutoRunError, match="already running"):
            await runner.start("other.sh", notify=False)
    finally:
        await runner.stop(grace=1)


@pytest.mark.asyncio
@needs_bash
async def test_stop_terminates_the_run(runner, sandbox):
    write_script(sandbox, "slow.sh", 'echo "::step:: long"\nsleep 60\n')
    run = await runner.start("slow.sh", notify=False)
    await asyncio.sleep(0.5)

    result = await runner.stop(grace=2)
    assert result["stopped"] is True
    assert run.status == "stopped"
    assert run.steps[0].status == "stopped"


@pytest.mark.asyncio
@needs_bash
async def test_stop_kills_the_whole_process_tree(runner, sandbox):
    """The point of the process group: a child outliving the stop is the
    failure that leaves a GPU held by a run you believe is over."""
    marker = sandbox / "child-alive.txt"
    write_script(
        sandbox,
        "parent.sh",
        # A grandchild that would keep touching the file if it survived.
        f'bash -c \'while true; do touch "{marker.as_posix()}"; sleep 0.2; done\' &\n'
        "sleep 60\n",
    )
    await runner.start("parent.sh", notify=False)
    await asyncio.sleep(1.0)
    assert marker.exists(), "grandchild never started; test cannot prove anything"

    await runner.stop(grace=2)
    await asyncio.sleep(0.5)
    marker.unlink()
    await asyncio.sleep(1.0)
    assert not marker.exists(), "grandchild survived the stop"


@pytest.mark.asyncio
async def test_stop_with_nothing_running_is_not_an_error(runner):
    assert await runner.stop() == {"stopped": False, "note": "nothing is running"}


@pytest.mark.asyncio
@needs_bash
async def test_bad_cwd_is_rejected(runner, sandbox):
    write_script(sandbox, "x.sh", "true\n")
    with pytest.raises(AutoRunError, match="working directory"):
        await runner.start("x.sh", cwd=str(sandbox / "missing"), notify=False)


@pytest.mark.asyncio
@needs_bash
async def test_bad_markers_value_is_rejected(runner, sandbox):
    write_script(sandbox, "x.sh", "true\n")
    with pytest.raises(AutoRunError, match="markers must be"):
        await runner.start("x.sh", markers="sometimes", notify=False)


# --------------------------------------------------------------- notifications
@pytest.mark.asyncio
async def test_notifier_is_inert_without_configuration(monkeypatch):
    from app.services.notify import TelegramNotifier

    monkeypatch.setattr(settings, "notify_enabled", True)
    monkeypatch.setattr(settings, "telegram_bot_token", "")
    n = TelegramNotifier()
    assert n.enabled is False
    assert await n.send("hello") is False


@pytest.mark.asyncio
async def test_notifier_swallows_transport_failure(monkeypatch):
    """A dead network must not propagate into the run being reported on."""
    from app.services.notify import TelegramNotifier

    monkeypatch.setattr(settings, "notify_enabled", True)
    monkeypatch.setattr(settings, "telegram_bot_token", "123:ABC")
    monkeypatch.setattr(settings, "telegram_chat_id", "42")

    n = TelegramNotifier()
    monkeypatch.setattr(
        n, "_post", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("unreachable"))
    )
    assert await n.send("hi") is False
    assert "unreachable" in n.last_error


@pytest.mark.asyncio
async def test_bot_token_never_appears_in_an_error(monkeypatch):
    from app.services.notify import TelegramNotifier

    token = "7654321:SUPERSECRETVALUE"
    monkeypatch.setattr(settings, "notify_enabled", True)
    monkeypatch.setattr(settings, "telegram_bot_token", token)
    monkeypatch.setattr(settings, "telegram_chat_id", "42")

    n = TelegramNotifier()
    monkeypatch.setattr(
        n, "_post", lambda *a, **k: (_ for _ in ()).throw(RuntimeError(f"boom {token}"))
    )
    await n.send("hi")
    assert token not in n.last_error
    assert "***" in n.last_error


def test_status_never_exposes_the_token(monkeypatch):
    from app.services.notify import TelegramNotifier

    monkeypatch.setattr(settings, "telegram_bot_token", "7654321:SUPERSECRET")
    monkeypatch.setattr(settings, "telegram_chat_id", "42")
    blob = repr(TelegramNotifier().status())
    assert "SUPERSECRET" not in blob
    assert "has_token" in blob


def test_html_escaping_of_script_output():
    from app.services.notify import esc

    assert esc("<script>a & b</script>") == "&lt;script&gt;a &amp; b&lt;/script&gt;"


def test_failure_message_names_the_step_and_code(runner):
    run = make_run(runner)
    step = Step(index=1, name="DAG", status="failed", rc=1, finished_at=time.time())
    run.steps.append(step)
    runner._tail.extend(["Traceback (most recent call last):", "ValueError: bad shard"])

    msg = runner._failure_message(run, step)
    assert "DAG" in msg and "exit <b>1</b>" in msg and "ValueError: bad shard" in msg


def test_summary_message_lists_every_step(runner):
    run = make_run(runner)
    run.status, run.exit_code, run.finished_at = "failed", 1, time.time()
    run.steps = [
        Step(index=1, name="dmsf", status="ok", rc=0, finished_at=time.time()),
        Step(index=2, name="DAG", status="failed", rc=1, finished_at=time.time()),
    ]
    msg = runner._summary_message(run)
    assert "FAILED" in msg and "dmsf" in msg and "DAG" in msg and "1/2 steps ok" in msg


# ------------------------------------------------------------------- endpoints
def test_status_endpoint_reports_idle(client, auth):
    body = client.get("/autorun/status", headers=auth).json()
    assert body["running"] is False and body["active"] is None
    assert body["notify"]["channel"] == "telegram"


def test_endpoints_require_a_token(client):
    assert client.get("/autorun/status").status_code == 401
    assert client.post("/autorun/start", json={"script": "x.sh"}).status_code == 401


def test_start_rejects_a_missing_script(client, auth):
    r = client.post("/autorun/start", json={"script": "does-not-exist.sh"}, headers=auth)
    assert r.status_code == 400
    assert "no such script" in r.json()["detail"]


def test_start_rejects_a_path_outside_the_sandbox(client, auth):
    r = client.post("/autorun/start", json={"script": "/etc/passwd"}, headers=auth)
    assert r.status_code == 400
    assert "outside AUTORUN_DIR" in r.json()["detail"]


def test_stop_with_nothing_running_returns_a_note(client, auth):
    body = client.post("/autorun/stop", json={}, headers=auth).json()
    assert body["stopped"] is False


def test_missing_run_log_is_a_404(client, auth):
    assert client.get("/autorun/runs/nope/log", headers=auth).status_code == 404


def test_notify_test_reports_missing_configuration(client, auth):
    body = client.post("/autorun/notify/test", headers=auth).json()
    assert body["ok"] is False
    assert "TELEGRAM_BOT_TOKEN" in body["error"]
