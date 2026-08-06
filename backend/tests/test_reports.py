"""Result analysis: the palette port, the log parser, and the /reports flow.

The palette tests are the important ones. `guides/visual_guide.md` §3 says to
validate by computing, gives the exact numbers a correct port must produce, and
warns that a port reporting anything else is wrong -- so those numbers are
pinned here rather than trusted.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.reports import palette as pal
from app.reports import parse as parsemod
from app.ssh import gateway


# ----------------------------------------------------------------- palette
def test_palette_matches_the_guides_sanity_numbers() -> None:
    """§3: 8 slots on the adjacent pairlist -> worst CVD 9.1, worst normal 19.6."""
    report = pal.validate(pairing="adjacent")
    assert round(report.worst_cvd, 1) == 9.1
    assert round(report.worst_normal, 1) == 19.6


def test_palette_first_three_slots_all_pairs() -> None:
    """§3: slots 1-3, every pair -> 9.2 and 24.0."""
    report = pal.validate(pal.SLOTS_LIGHT[:3], pairing="all")
    assert round(report.worst_cvd, 1) == 9.2
    assert round(report.worst_normal, 1) == 24.0


def test_every_slot_clears_the_band_and_chroma_floor() -> None:
    report = pal.validate()
    assert report.ok, report.failures()
    assert all(pal.L_BAND["light"][0] <= s.lightness <= pal.L_BAND["light"][1]
               for s in report.slots)
    assert all(s.chroma >= pal.CHROMA_FLOOR for s in report.slots)


def test_the_three_relief_slots_are_the_ones_the_guide_names() -> None:
    """Aqua 2.74, yellow 2.11, magenta 2.62 sit under 3:1 and need labels."""
    report = pal.validate()
    by_index = {s.index: s for s in report.slots}
    assert report.relief_required == [3, 4, 5]
    assert round(by_index[3].contrast, 2) == 2.74
    assert round(by_index[4].contrast, 2) == 2.11
    assert round(by_index[5].contrast, 2) == 2.62


def test_dark_slots_clear_their_own_band() -> None:
    report = pal.validate(theme="dark", pairing="adjacent")
    assert all(s.in_band for s in report.slots), report.failures()


def test_series_colors_refuse_to_cycle() -> None:
    """A 9th series is never a generated hue (§1); all-pairs forms cap at 3."""
    assert pal.series_colors(3) == list(pal.SLOTS_LIGHT[:3])
    with pytest.raises(ValueError, match="fold into"):
        pal.series_colors(4, all_pairs=True)
    with pytest.raises(ValueError):
        pal.series_colors(9)


def test_cvd_simulation_collapses_red_and_green() -> None:
    """Sanity on the Machado matrices themselves, independent of the palette."""
    plain = pal.delta_e("#e34948", "#008300")
    simulated = pal.cvd_delta_e("#e34948", "#008300")
    assert simulated < plain


# ------------------------------------------------------------------ parser
def parse_text(text: str) -> dict[str, list[float]]:
    samples, _ = parsemod.parse_log(text, "test.log")
    out: dict[str, list[float]] = {}
    for s in samples:
        out.setdefault(s.metric, []).append(s.value)
    return out


def test_adjacent_key_values_do_not_eat_each_other() -> None:
    """`edge_ms=12.4 msg_mb=0.19` must not parse as `ms` and `mb`."""
    found = parse_text("frame=1 edge_ms=12.4 msg_mb=0.19 utilization=37.82%")
    assert found == {"edge_ms": [12.4], "msg_mb": [0.19], "utilization": [37.82]}


def test_percent_is_stripped_and_recorded_as_a_unit() -> None:
    samples, _ = parsemod.parse_log("utilization=37.82%", "a.log")
    assert samples[0].value == 37.82
    assert samples[0].unit == "%"


def test_dates_versions_and_addresses_are_not_metrics() -> None:
    found = parse_text(
        "2026-07-28 14:32:01 INFO starting agent v2.1.0 on 192.168.1.10:5672\n"
    )
    assert found == {}


def test_counters_and_prose_are_skipped() -> None:
    assert parse_text("frame=7") == {}
    assert parse_text("Loading checkpoint 3") == {}
    assert parse_text("epoch 3 done") == {}


def test_loose_form_needs_a_real_unit() -> None:
    assert parse_text("Throughput 21.3 fps") == {"throughput": [21.3]}
    assert parse_text("Throughput 21.3 widgets") == {}


def test_aligned_profiler_columns_parse() -> None:
    assert parse_text("Average latency     12.35 ms") == {"average_latency": [12.35]}


def test_timestamps_become_elapsed_seconds() -> None:
    samples, _ = parsemod.parse_log(
        "[14:32:01] a=1\n[14:32:03] a=2\n", "t.log"
    )
    assert [s.at for s in samples] == [0.0, 2.0]


def test_parse_tree_reads_logs_csv_and_skips_binaries(tmp_path: Path) -> None:
    (tmp_path / "edge.log").write_text("edge_ms=12.4\nedge_ms=11.9\n", encoding="utf-8")
    (tmp_path / "summary.csv").write_text("run,e2e_ms\n1,46.7\n2,48.1\n", encoding="utf-8")
    (tmp_path / "head.pt").write_bytes(b"\x00\x01" * 64)

    result = parsemod.parse_tree(tmp_path)
    assert {f.path for f in result.read_files} == {"edge.log", "summary.csv"}
    assert "head.pt" in result.skipped
    assert result.metrics["edge_ms"].count == 2
    assert result.metrics["e2e_ms"].values == [46.7, 48.1]
    # `run` is an index column, not a result.
    assert "run" not in result.metrics


# ------------------------------------------------------------------ charts
def test_render_draws_files_and_a_tile(tmp_path: Path) -> None:
    from app.reports.charts import render

    source = tmp_path / "in"
    source.mkdir()
    (source / "edge.log").write_text(
        "\n".join(f"edge_ms={12 + (i % 7) * 0.3:.2f}" for i in range(40))
        + "\nPeak memory 1284 MB\n",
        encoding="utf-8",
    )
    charts, tiles, _ = render(parsemod.parse_tree(source), tmp_path / "imgs")

    assert charts and all((tmp_path / "imgs" / c.file).is_file() for c in charts)
    # Numbered so a file listing keeps them in narrative order (§6).
    assert charts[0].file.startswith("01_")
    # One reading in one file is a stat tile, never a one-bar bar chart (§0).
    assert [t.label for t in tiles] == ["Peak memory"]


def test_identical_series_are_not_drawn_twice(tmp_path: Path) -> None:
    """§6: two runs with identical values must not imply a comparison."""
    from app.reports.charts import render

    source = tmp_path / "in"
    source.mkdir()
    body = "\n".join(f"lat_ms={10 + i}" for i in range(12)) + "\n"
    (source / "a.log").write_text(body, encoding="utf-8")
    (source / "b.log").write_text(body, encoding="utf-8")

    charts, _, _ = render(parsemod.parse_tree(source), tmp_path / "imgs")
    trend = next(c for c in charts if c.kind == "trend")
    assert "identical" in trend.subtitle


# --------------------------------------------------------------- endpoints
@pytest.fixture()
def configured(client, auth, ui_export):
    client.post("/seed", json=ui_export, headers=auth)
    client.post(
        "/server/config",
        json={"ip": "10.0.0.9", "ssh_user": "dai", "ssh_password": "pw"},
        headers=auth,
    )
    return client


def fake_pull(files: dict[str, str]):
    """Stand in for the SFTP walk by writing the files into the staging dir."""

    async def pull(_conn, remote, local: Path, **_kw):
        local.mkdir(parents=True, exist_ok=True)
        for name, body in files.items():
            (local / name).write_text(body, encoding="utf-8")
        return {
            "root": remote,
            "files": [{"path": n, "bytes": len(b)} for n, b in files.items()],
            "bytes": sum(len(b) for b in files.values()),
            "skipped": [], "truncated": "",
        }

    return pull


LOG_A = "\n".join(f"edge_ms={12 + (i % 5) * 0.4:.2f}" for i in range(40)) + "\nPeak memory 1284 MB\n"
LOG_B = "\n".join(f"cloud_ms={4 + (i % 3) * 0.5:.2f}" for i in range(40)) + "\nPeak memory 2210 MB\n"


def analyze(configured, auth, case="cut6-8bit", **extra):
    with patch("app.ssh.pool.pool.get", new=AsyncMock()), patch(
        "app.ssh.commands.sftp_get_dir",
        new=AsyncMock(side_effect=fake_pull({"a.log": LOG_A, "b.log": LOG_B})),
    ):
        return configured.post(
            "/reports/analyze",
            json={"device_id": gateway.SERVER_DEVICE_ID, "path": "/home/dai/run7",
                  "case_name": case, **extra},
            headers=auth,
        )


def test_analyze_charts_a_directory_and_names_the_report(configured, auth) -> None:
    body = analyze(configured, auth).json()

    assert body["case_name"] == "cut6-8bit"
    assert body["id"].endswith("_cut6-8bit")
    # day-month-hour-minute, then the case name.
    stamp = body["id"].split("_")[0]
    assert len(stamp) == 9 and stamp[4] == "-"
    assert body["charts"] and all(c["note"] == "" for c in body["charts"])
    assert body["source_path"] == "/home/dai/run7"
    assert {f["path"] for f in body["files"]} == {"a.log", "b.log"}


def test_chart_images_are_served(configured, auth) -> None:
    body = analyze(configured, auth).json()
    first = body["charts"][0]["file"]

    ok = configured.get(f"/reports/{body['id']}/imgs/{first}", headers=auth)
    assert ok.status_code == 200
    assert ok.headers["content-type"] == "image/png"
    assert ok.content[:8] == b"\x89PNG\r\n\x1a\n"

    escape = configured.get(f"/reports/{body['id']}/imgs/..%2Fmanifest.json", headers=auth)
    assert escape.status_code == 404


def test_notes_are_saved_against_each_chart(configured, auth) -> None:
    body = analyze(configured, auth).json()
    chart_id = body["charts"][0]["id"]

    saved = configured.put(
        f"/reports/{body['id']}/notes",
        json={"notes": {chart_id: "edge is stable"}, "review": "good run"},
        headers=auth,
    ).json()

    assert saved["review"] == "good run"
    assert next(c for c in saved["charts"] if c["id"] == chart_id)["note"] == "edge is stable"
    assert saved["saved_at"]

    # …and survive a reload, which is the entire point of saving them.
    again = configured.get(f"/reports/{body['id']}", headers=auth).json()
    assert next(c for c in again["charts"] if c["id"] == chart_id)["note"] == "edge is stable"


def test_reports_are_listed_newest_first_and_deletable(configured, auth) -> None:
    first = analyze(configured, auth, case="baseline").json()
    second = analyze(configured, auth, case="cut8").json()

    listed = configured.get("/reports", headers=auth).json()["reports"]
    ids = [r["id"] for r in listed]
    assert second["id"] in ids and first["id"] in ids
    assert ids.index(second["id"]) <= ids.index(first["id"])

    assert configured.delete(f"/reports/{first['id']}", headers=auth).status_code == 200
    assert configured.get(f"/reports/{first['id']}", headers=auth).status_code == 404


def test_listing_carries_day_and_time_for_the_history_bar(configured, auth) -> None:
    body = analyze(configured, auth).json()
    row = next(
        r for r in configured.get("/reports", headers=auth).json()["reports"]
        if r["id"] == body["id"]
    )

    assert row["day"] == body["created_at"][:10]        # sortable, ISO
    assert len(row["time_label"]) == 5                  # 14:32
    assert row["day_label"][:2].isdigit()               # 28 Jul


def test_day_groups_label_today_and_yesterday(configured, auth) -> None:
    analyze(configured, auth)
    days = configured.get("/reports", headers=auth).json()["days"]

    assert days and days[0]["label"] == "Today"
    assert days[0]["count"] >= 1


def test_day_groups_are_newest_first_and_survive_a_filter(configured, auth) -> None:
    from app.reports import store

    rows = [
        {"day": "2026-07-26", "day_label": "26 Jul"},
        {"day": "2026-07-28", "day_label": "28 Jul"},
        {"day": "2026-07-28", "day_label": "28 Jul"},
        {"day": "2026-08-01", "day_label": "01 Aug"},
    ]
    groups = store.day_groups(rows, today=date(2026, 8, 2))

    # Newest first, by the ISO day -- `2807` would sort after `0108` as a label.
    assert [g["day"] for g in groups] == ["2026-08-01", "2026-07-28", "2026-07-26"]
    assert [g["count"] for g in groups] == [1, 2, 1]
    assert [g["label"] for g in groups] == ["Yesterday", "28 Jul", "26 Jul"]


def test_day_filter_narrows_reports_but_not_the_day_chips(configured, auth) -> None:
    body = analyze(configured, auth).json()
    day = body["created_at"][:10]

    filtered = configured.get(f"/reports?day={day}", headers=auth).json()
    assert filtered["reports"] and all(r["day"] == day for r in filtered["reports"])
    assert any(g["day"] == day for g in filtered["days"])

    empty = configured.get("/reports?day=1999-01-01", headers=auth).json()
    assert empty["reports"] == []
    # The bar must still say which days do have results.
    assert empty["days"]


def test_analyze_keeps_the_logs_so_charts_can_be_redrawn(configured, auth) -> None:
    """The config panel re-renders from these, never over SSH again."""
    from app.reports import store

    body = analyze(configured, auth).json()
    kept = store.logs_dir(body["id"])

    assert {p.name for p in kept.iterdir()} == {"a.log", "b.log"}
    assert body["pulled"]["kept"] == 2
    assert body["pulled"]["kept_bytes"] > 0


def test_views_redraw_the_report_and_keep_the_notes(configured, auth) -> None:
    body = analyze(configured, auth).json()
    chart = next(c for c in body["charts"] if c["series"])
    series = chart["series"][0]["key"]

    configured.put(
        f"/reports/{body['id']}/notes",
        json={"notes": {chart["id"]: "keep me"}, "review": "keep me too"},
        headers=auth,
    )

    redrawn = configured.put(
        f"/reports/{body['id']}/views",
        json={"views": {chart["id"]: {
            "title": "Renamed", "xlabel": "x here", "ylabel": "y here",
            "hidden": [series],
        }}},
        headers=auth,
    ).json()

    edited = next(c for c in redrawn["charts"] if c["id"] == chart["id"])
    assert edited["title"] == "Renamed"
    assert edited["view"]["hidden"] == [series]
    # The note and the review are the only parts a person typed; a re-render
    # must not take them with it.
    assert edited["note"] == "keep me"
    assert redrawn["review"] == "keep me too"
    # …and the stamp moved, so the browser refetches the changed PNG.
    assert redrawn["rendered_at"] >= body["rendered_at"]

    # It survives a reload, like the notes do.
    again = configured.get(f"/reports/{body['id']}", headers=auth).json()
    assert next(c for c in again["charts"] if c["id"] == chart["id"])["title"] == "Renamed"


def test_an_empty_views_map_puts_every_chart_back(configured, auth) -> None:
    body = analyze(configured, auth).json()
    chart = next(c for c in body["charts"] if c["series"])

    configured.put(
        f"/reports/{body['id']}/views",
        json={"views": {chart["id"]: {"title": "Renamed"}}}, headers=auth,
    )
    reset = configured.put(
        f"/reports/{body['id']}/views", json={"views": {}}, headers=auth
    ).json()

    back = next(c for c in reset["charts"] if c["id"] == chart["id"])
    assert back["title"] == back["defaults"]["title"]
    assert back["view"] == {"title": "", "xlabel": "", "ylabel": "", "hidden": []}


def test_views_on_a_report_without_kept_logs_is_a_clear_409(configured, auth) -> None:
    """Reports analysed before the logs were kept cannot be re-drawn."""
    import shutil

    from app.reports import store

    body = analyze(configured, auth).json()
    shutil.rmtree(store.logs_dir(body["id"]))

    r = configured.put(f"/reports/{body['id']}/views", json={"views": {}}, headers=auth)
    assert r.status_code == 409
    assert "without its logs" in r.json()["detail"]


def test_views_reject_a_bad_report_id(configured, auth) -> None:
    assert configured.put("/reports/nope/views", json={"views": {}},
                          headers=auth).status_code == 404
    # A traversal attempt never reaches the handler: `%2E%2E` survives URL
    # normalisation, so this is the case the id guard actually sees.
    assert configured.put("/reports/%2E%2E/views", json={"views": {}},
                          headers=auth).status_code == 400


def test_analyze_records_the_window_it_charted(configured, auth) -> None:
    """A report says which slice of the run it is, or it is not reproducible."""
    whole = analyze(configured, auth, case="all").json()
    part = analyze(configured, auth, case="mid",
                   window_start=5, window_end=90).json()

    assert whole["window"] == {}
    assert part["window"] == {"start": 5.0, "end": 90.0, "label": "5–90%"}
    # 40 readings per log, so 5–90% is readings 2 through 36 of each.
    assert "35 readings" in next(c for c in part["charts"] if c["kind"] == "trend")["summary"]
    assert "40 readings" in next(c for c in whole["charts"] if c["kind"] == "trend")["summary"]


def test_the_window_survives_a_redraw(configured, auth) -> None:
    """Renaming an axis must not quietly widen the report to the whole run."""
    body = analyze(configured, auth, window_start=5, window_end=90).json()
    chart = next(c for c in body["charts"] if c["kind"] == "trend")

    redrawn = configured.put(
        f"/reports/{body['id']}/views",
        json={"views": {chart["id"]: {"title": "Renamed"}}}, headers=auth,
    ).json()

    assert redrawn["window"] == body["window"]
    again = next(c for c in redrawn["charts"] if c["id"] == chart["id"])
    assert again["title"] == "Renamed"
    assert again["summary"] == chart["summary"]        # the same 35 readings


def test_the_window_shows_up_in_the_history_row(configured, auth) -> None:
    body = analyze(configured, auth, case="mid", window_start=5, window_end=90).json()
    whole = analyze(configured, auth, case="all").json()
    rows = {r["id"]: r for r in configured.get("/reports", headers=auth).json()["reports"]}

    assert rows[body["id"]]["window"] == "5–90%"
    assert rows[whole["id"]]["window"] == ""


@pytest.mark.parametrize(
    "bounds",
    [(90, 5), (50, 50), (-1, 90), (5, 101)],
    ids=["inverted", "empty", "below-zero", "over-a-hundred"],
)
def test_a_window_that_is_not_a_slice_of_a_run_is_refused(configured, auth, bounds) -> None:
    """Widening it to the whole run instead would answer a different question."""
    r = analyze(configured, auth, window_start=bounds[0], window_end=bounds[1])
    assert r.status_code == 422


def test_analyze_rejects_a_directory_with_nothing_readable(configured, auth) -> None:
    with patch("app.ssh.pool.pool.get", new=AsyncMock()), patch(
        "app.ssh.commands.sftp_get_dir", new=AsyncMock(side_effect=fake_pull({}))
    ):
        r = configured.post(
            "/reports/analyze",
            json={"device_id": gateway.SERVER_DEVICE_ID, "path": "/home/dai/empty",
                  "case_name": "empty"},
            headers=auth,
        )
    assert r.status_code == 400
    assert "no readable result files" in r.json()["detail"]


def test_analyze_applies_the_same_path_guard_as_pull(configured, auth) -> None:
    for path in ("/opt/../../etc", "/home/dai/.ssh/id_rsa"):
        r = configured.post(
            "/reports/analyze",
            json={"device_id": gateway.SERVER_DEVICE_ID, "path": path, "case_name": "x"},
            headers=auth,
        )
        assert r.status_code == 400, path
