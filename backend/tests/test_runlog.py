"""The split-inference result directory: reading it, and charting it.

The line formats pinned here are copied out of `guides/server_results_guide.md`
§2 verbatim. That is the point of the file -- the reader exists because those
formats carry `cluster=`, `role=` and `kind=`, and a parser that drops them can
only compare files, which is what the charts used to do.

`guides/visual_guide.md` supplies the rest: the catalogue in Part II, the
numbering rule in §III.5, and the §III.4 instruction to check whether two series
are identical before drawing them as two lines.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from app.reports import charts as chartmod
from app.reports import parse as parsemod
from app.reports import runcharts, runlog
from app.reports.window import Window

# --------------------------------------------------------------- the fixture
# Small but structurally complete: two clusters, both roles, both mAP
# pipelines, and an adaptive cut change.
FPS_CLUSTER = """\
1785128504858762215 cluster=intermediate_queue_0 fps=18.131 steady_fps=18.491 done=336 frames=10752 share=66.7%
1785128504858762215 cluster=intermediate_queue_1 fps=8.407 steady_fps=8.740 done=168 frames=5376 share=33.3%
1785128504858762215 SYSTEM fps=25.220 done=504 frames=16128 clusters=2
"""

# `<ns>` while warming up, `<ns> <fps>` after -- §2.1. The bare rows are real
# events with no reading yet.
#
# Long enough that `smooth_window` returns a width, so the rolling mean the
# timeline draws over the raw readings is exercised rather than skipped.
BATCH_DONE = "\n".join(
    [f"{1785127877009606331 + i * 40_000_000}" for i in range(3)]
    + [f"{1785127877009606331 + i * 40_000_000} {20 + i % 5}.10" for i in range(3, 33)]
) + "\n"

FPS_CLUSTER_NS = "\n".join(
    f"{1785127877009606331 + i * 40_000_000} "
    f"cluster=intermediate_queue_{i % 2} done={i // 2 + 1}"
    + (f" window_fps={12 + (i % 7)}.25" if i >= 4 else "")
    for i in range(40)
) + "\n"

UTILIZATION = """\
1785128504862965418 client=aaa role=edge packages=56 busy_s=176.760 total_s=467.394 utilization=37.82%
1785128504862965418 client=bbb role=edge packages=61 busy_s=210.100 total_s=470.000 utilization=44.70%
1785128504862965418 client=ccc role=cloud packages=120 busy_s=430.000 total_s=460.000 utilization=93.48%
1785128504862965418 client=ddd role=cloud packages=118 busy_s=421.000 total_s=459.000 utilization=91.72%
"""

UTILIZATION_CLUSTER = """\
1785128504874248492 cluster=intermediate_queue_0 ALL devices=8 utilization=55.06% utilization_mean=53.29% busy_s=2348.297 total_s=4264.621 packages=672
1785128504874248492 cluster=intermediate_queue_0 role=cloud devices=2 utilization=93.44% busy_s=1119.832 total_s=1198.399 packages=336
1785128504874248492 cluster=intermediate_queue_0 role=edge devices=6 utilization=41.20% busy_s=1228.465 total_s=2981.222 packages=336
1785128504874248492 cluster=intermediate_queue_1 ALL devices=3 utilization=48.10% utilization_mean=47.80% busy_s=700.000 total_s=1455.000 packages=168
1785128504874248492 cluster=intermediate_queue_1 role=cloud devices=1 utilization=90.10% busy_s=410.000 total_s=455.000 packages=84
1785128504874248492 cluster=intermediate_queue_1 role=edge devices=2 utilization=29.00% busy_s=290.000 total_s=1000.000 packages=84
1785128504874248492 SYSTEM devices=11 clusters=2 utilization=53.20% utilization_mean=51.80% busy_s=3048.297 total_s=5719.621
"""

LATENCY_CLUSTER = """\
1785128504874248492 cluster=intermediate_queue_0 role=cloud kind=service n=336 mean_ms=3332.833 p50_ms=3470.922 p95_ms=3966.246 max_ms=4367.941
1785128504874248492 cluster=intermediate_queue_0 role=edge kind=service n=336 mean_ms=214.110 p50_ms=210.400 p95_ms=255.900 max_ms=301.200
1785128504874248492 cluster=intermediate_queue_0 kind=e2e n=336 mean_ms=69655.623 p50_ms=69379.207 p95_ms=102639.355 max_ms=111760.937
1785128504874248492 cluster=intermediate_queue_1 kind=e2e n=168 mean_ms=75000.000 p50_ms=74000.000 p95_ms=110000.000 max_ms=120000.000
1785128504874248492 SYSTEM kind=e2e n=504 mean_ms=71100.000 p50_ms=70500.000 p95_ms=104000.000 max_ms=120000.000
"""

MAP_WINDOW = "\n".join(
    f"1785128505579200616 cluster=intermediate_queue_{c} window={w} batches={w}-{w + 15} "
    f"frames=512 mAP50_95=0.0{780 + w * 7 + c} mAP50=0.1{465 + w * 9 + c * 2}"
    for c in (0, 1) for w in range(10)
) + "\n"

MAP_SUMMARY = """\
1785128505579200616 cluster=intermediate_queue_0 WINDOW mAP50_95=0.1004 mAP50=0.1857 (mean of 14 window(s) x 16 batches, step 1)
1785128505579200616 cluster=intermediate_queue_0 ALL    mAP50_95=0.0882 mAP50=0.1632 (905/905 GT frame(s) matched)
1785128505579200616 cluster=intermediate_queue_1 WINDOW mAP50_95=0.1008 mAP50=0.1861 (mean of 14 window(s) x 16 batches, step 1)
1785128505579200616 cluster=intermediate_queue_1 ALL    mAP50_95=0.0885 mAP50=0.1636 (905/905 GT frame(s) matched)
1785128505579200616 OVERALL WINDOW mAP50_95=0.1006 mAP50=0.1859 (avg over 2 cluster(s))
1785128505579200616 OVERALL ALL    mAP50_95=0.0883 mAP50=0.1634 (avg over 2 cluster(s))
"""

CUT_CHANGE = "1785127917009606331 intermediate_queue_0: cut 11->12 deeper\n"

CONFIG = "batch_size: 32\nnum_bit: 8\nmap:\n  window_batches: 16\n"

RESULT_FILES = {
    "fps_cluster.log": FPS_CLUSTER,
    "batch_done_ns.log": BATCH_DONE,
    "fps_cluster_ns.log": FPS_CLUSTER_NS,
    "utilization.log": UTILIZATION,
    "utilization_cluster.log": UTILIZATION_CLUSTER,
    "latency_cluster.log": LATENCY_CLUSTER,
    "map_window.log": MAP_WINDOW,
    "map.log": MAP_SUMMARY,
    "cut_change_ns.log": CUT_CHANGE,
    "config.yaml": CONFIG,
}


def write_run(root: Path, name: str = "results_0729_2204_dynamic", **overrides: str) -> Path:
    """A result directory on disk. `overrides` replaces or (with "") drops a file."""
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    for filename, body in {**RESULT_FILES, **overrides}.items():
        if body == "":
            (directory / filename).unlink(missing_ok=True)
            continue
        (directory / filename).write_text(body, encoding="utf-8")
    return directory


@pytest.fixture()
def run_dir(tmp_path: Path) -> Path:
    return write_run(tmp_path)


# ------------------------------------------------------------------- reading
def test_detect_needs_the_files_that_define_a_run(tmp_path: Path, run_dir: Path) -> None:
    assert runlog.detect(run_dir)
    # A directory of logs that merely *looks* log-shaped is not one of these.
    other = tmp_path / "other"
    other.mkdir()
    (other / "app.log").write_text("edge_ms=12.4\n", encoding="utf-8")
    assert not runlog.detect(other)


def test_tag_comes_from_the_archive_directory_name(tmp_path: Path) -> None:
    assert runlog.tag_of(Path("results_0729_2204_dynamic")) == "dynamic"
    assert runlog.tag_of(Path("results_0729_2204_split")) == "split"
    assert runlog.tag_of(Path("some_run")) == ""


def test_queue_ids_become_display_labels() -> None:
    """No chart code ever contains a raw queue name (§III.2)."""
    assert runlog.cluster_label("intermediate_queue_0") == "Cluster 0"
    assert runlog.cluster_label("queue_11") == "Cluster 11"
    assert runlog.cluster_label("custom-name") == "custom-name"


def test_throughput_keeps_the_cluster_and_system_scopes(run_dir: Path) -> None:
    data = runlog.read_run(run_dir)

    assert data.clusters == ["Cluster 0", "Cluster 1"]
    assert data.scopes == ["Cluster 0", "Cluster 1", "System"]
    assert data.throughput["Cluster 0"]["fps"] == 18.131
    assert data.throughput["Cluster 0"]["steady_fps"] == 18.491
    # `share=66.7%` -- the percent is stripped at the edge, not at the label.
    assert data.throughput["Cluster 0"]["share"] == 66.7
    assert data.system("fps") == 25.220
    # SYSTEM has no steady-state figure; nothing may invent one.
    assert "steady_fps" not in data.throughput["System"]


def test_batch_done_reads_the_positional_two_column_form(run_dir: Path) -> None:
    """§2.1: the first rows are `<ns>` alone. The old parser found nothing here."""
    data = runlog.read_run(run_dir)

    assert len(data.system_fps) == 30          # 33 lines, 3 of them warm-up
    assert data.system_fps[0].at > 0           # the clock starts at the first DONE
    assert data.base_ns == 1785127877009606331
    assert all(p.value > 0 for p in data.system_fps)


def test_window_fps_is_split_per_cluster(run_dir: Path) -> None:
    data = runlog.read_run(run_dir)

    assert set(data.cluster_fps) == {"Cluster 0", "Cluster 1"}
    assert all(len(points) >= 8 for points in data.cluster_fps.values())
    # Both clusters are measured against one clock, so their times line up.
    assert data.cluster_fps["Cluster 0"][0].at != data.cluster_fps["Cluster 1"][0].at


def test_utilization_keeps_cluster_role_and_device_rows(run_dir: Path) -> None:
    data = runlog.read_run(run_dir)

    assert data.roles == ["cloud", "edge"]     # pipeline order, not alphabetical
    assert data.utilization[("Cluster 0", "cloud")]["utilization"] == 93.44
    assert data.utilization[("Cluster 0", "all")]["utilization_mean"] == 53.29
    assert data.utilization[("System", "all")]["devices"] == 11
    assert len(data.devices) == 4
    assert {str(d["role"]) for d in data.devices} == {"edge", "cloud"}


def test_latency_separates_service_from_e2e(run_dir: Path) -> None:
    """`service` is per role, `e2e` is per cluster -- merging them is meaningless."""
    data = runlog.read_run(run_dir)

    assert data.latency[("Cluster 0", "cloud", "service")]["mean_ms"] == 3332.833
    assert data.latency[("Cluster 0", "edge", "service")]["mean_ms"] == 214.110
    # e2e carries no role, so it lands under "all" rather than colliding.
    assert data.latency[("Cluster 0", "all", "e2e")]["p95_ms"] == 102639.355
    assert data.latency[("System", "all", "e2e")]["n"] == 504


def test_accuracy_keeps_the_two_pipelines_apart(run_dir: Path) -> None:
    """WINDOW and ALL answer different questions (§2.8); ALL is the headline."""
    data = runlog.read_run(run_dir)

    assert data.accuracy[("Overall", "ALL")]["mAP50"] == 0.1634
    assert data.accuracy[("Overall", "WINDOW")]["mAP50"] == 0.1859
    assert data.accuracy[("Cluster 0", "ALL")]["mAP50_95"] == 0.0882
    assert len(data.accuracy_window["Cluster 0"]) == 10
    assert [r["window"] for r in data.accuracy_window["Cluster 1"]] == list(range(10))


def test_cut_changes_are_read_onto_the_run_clock(run_dir: Path) -> None:
    data = runlog.read_run(run_dir)

    assert len(data.cuts) == 1
    cut = data.cuts[0]
    assert (cut["from"], cut["to"], cut["direction"]) == (11, 12, "deeper")
    assert cut["cluster"] == "Cluster 0"
    # 40 s after the first DONE, on the same clock as the FPS series -- which
    # is what lets the marker land in the right place on the timeline.
    assert cut["at"] == pytest.approx(40.0, abs=1e-6)
    assert data.system_fps[0].at < cut["at"]


def test_a_stale_cut_log_is_ignored_outside_an_adaptive_run(tmp_path: Path) -> None:
    """§2.9: a `split` archive can carry a previous dynamic run's file."""
    directory = write_run(tmp_path, name="results_0729_2130_split")

    data = runlog.read_run(directory)
    assert data.tag == "split"
    assert data.cuts == []


def test_a_missing_file_is_noted_and_never_raises(tmp_path: Path) -> None:
    directory = write_run(tmp_path, **{"map.log": "", "map_window.log": ""})

    data = runlog.read_run(directory)
    assert not data.has_accuracy
    assert any("map.log" in w for w in data.warnings)
    # …and everything else still read.
    assert data.system("fps") == 25.220


def test_a_corrupt_line_does_not_sink_the_file(tmp_path: Path) -> None:
    directory = write_run(tmp_path, **{
        "fps_cluster.log": "not a log line at all\n" + FPS_CLUSTER + "\ncluster= fps=\n"
    })

    data = runlog.read_run(directory)
    assert data.system("fps") == 25.220


# ------------------------------------------------------------------ charting
def render(directory: Path, out: Path):
    return chartmod.render(parsemod.parse_tree(directory), out, source_dir=directory)


def test_the_catalogue_is_drawn_for_a_result_directory(run_dir: Path, tmp_path: Path) -> None:
    charts, tiles, _ = render(run_dir, tmp_path / "imgs")

    ids = [c.id for c in charts]
    assert ids == [
        "throughput_by_cluster", "system_window_fps", "cluster_window_fps",
        "window_fps_distribution", "service_latency_by_role", "e2e_latency_profile",
        "utilization_by_role", "device_utilization", "map_by_window", "map_summary",
    ]
    assert all((tmp_path / "imgs" / c.file).is_file() for c in charts)
    # None of the schema-blind forms may appear: they compare files, and these
    # files are not alternatives to each other.
    assert "comparison" not in ids and "delta" not in ids
    assert tiles


def test_chart_numbers_are_fixed_not_sequential(tmp_path: Path) -> None:
    """§III.5: a dropped chart leaves its gap so a saved note keeps its meaning."""
    directory = write_run(tmp_path, **{"map.log": "", "map_window.log": ""})

    charts, _, _ = render(directory, tmp_path / "imgs")
    by_id = {c.id: c.file for c in charts}

    assert len(charts) == 8
    assert by_id["utilization_by_role"].startswith("07_")
    assert by_id["device_utilization"].startswith("08_")
    assert not any(c.file.startswith(("09_", "10_")) for c in charts)


def test_identical_cluster_accuracy_is_drawn_once_and_said_out_loud(tmp_path: Path) -> None:
    """§III.4: two overlapping lines imply a comparison that is not in the data."""
    same = "\n".join(
        f"1785128505579200616 cluster=intermediate_queue_{c} window={w} batches={w}-{w + 15} "
        f"frames=512 mAP50_95=0.0787 mAP50=0.1465"
        for c in (0, 1) for w in range(10)
    ) + "\n"
    directory = write_run(tmp_path, **{"map_window.log": same})

    charts, _, _ = render(directory, tmp_path / "imgs")
    window = next(c for c in charts if c.id == "map_by_window")
    assert "identical" in window.subtitle


def test_wide_figures_are_flagged_for_the_gallery(run_dir: Path, tmp_path: Path) -> None:
    charts, _, _ = render(run_dir, tmp_path / "imgs")
    by_id = {c.id: c for c in charts}

    assert by_id["system_window_fps"].wide          # a timeline needs the row
    assert not by_id["throughput_by_cluster"].wide  # a five-bar chart does not
    # …and it must survive `json.dumps`: numpy's bool does not (it is what
    # `get_figwidth` comparisons return).
    json.dumps([c.to_dict() for c in charts])


def test_tiles_carry_the_headline_numbers_with_their_context(run_dir: Path) -> None:
    """C12: the number that *is* the story, plus the words beside the color."""
    data = runlog.read_run(run_dir)
    by_label = {t.label: t for t in runcharts.tiles(data)}

    assert by_label["System throughput"].value == "25.2"
    assert by_label["System throughput"].unit == "FPS"
    assert by_label["Accuracy (all frames)"].value == "0.1634"
    assert by_label["End-to-end latency"].value == "71.1"
    # Pooled 53.2% against a 51.8% mean is inside the tolerance, so no alarm.
    assert by_label["System utilization"].delta_kind == ""
    assert by_label["Batch size"].value == "32"


def test_a_divergent_utilization_mean_is_called_out(tmp_path: Path) -> None:
    """§2.5: pooled and mean apart means one group is carrying the others."""
    directory = write_run(tmp_path, **{
        "utilization_cluster.log": UTILIZATION_CLUSTER.replace(
            "SYSTEM devices=11 clusters=2 utilization=53.20% utilization_mean=51.80%",
            "SYSTEM devices=11 clusters=2 utilization=53.20% utilization_mean=31.80%",
        )
    })

    tile = next(t for t in runcharts.tiles(runlog.read_run(directory))
                if t.label == "System utilization")
    assert tile.delta_kind == "bad"
    assert "imbalanced" in tile.delta       # never color alone (§1)


# ------------------------------------------------------- per-chart config
def test_every_chart_declares_what_it_can_hide(run_dir: Path, tmp_path: Path) -> None:
    """The config panel can only offer what a chart body knows how to switch off."""
    charts, _, _ = render(run_dir, tmp_path / "imgs")
    keys = {c.id: [s.key for s in c.series] for c in charts}

    assert keys["throughput_by_cluster"] == ["fps", "steady_fps"]
    assert keys["cluster_window_fps"] == ["Cluster 0", "Cluster 1"]
    assert keys["utilization_by_role"] == ["cloud", "edge", "all"]
    assert keys["e2e_latency_profile"] == ["mean_ms", "p50_ms", "p95_ms", "max_ms"]
    # The timeline's reference line and its cut markers are switchable too.
    assert set(keys["system_window_fps"]) == {"reading", "mean", "average", "cuts"}
    # Every chart offers something, and every series has a label to show.
    assert all(keys[c.id] for c in charts)
    assert all(s.label for c in charts for s in c.series)


def test_defaults_are_recorded_so_reset_is_visible(run_dir: Path, tmp_path: Path) -> None:
    charts, _, _ = render(run_dir, tmp_path / "imgs")
    timeline = next(c for c in charts if c.id == "system_window_fps")

    assert timeline.defaults == {
        "title": "System throughput over the run",
        "xlabel": "seconds into the run",
        "ylabel": "Rolling window FPS",
    }
    assert timeline.title == timeline.defaults["title"]


def test_a_view_renames_the_chart_and_drops_a_series(run_dir: Path, tmp_path: Path) -> None:
    from app.reports.style import View

    views = {"system_window_fps": View(
        title="Throughput, smoothed", xlabel="elapsed (s)", ylabel="FPS",
        hidden=("reading", "cuts"),
    )}
    charts, _, _ = chartmod.render(
        parsemod.parse_tree(run_dir), tmp_path / "imgs",
        source_dir=run_dir, views=views,
    )
    timeline = next(c for c in charts if c.id == "system_window_fps")

    assert timeline.title == "Throughput, smoothed"
    assert timeline.view.hidden == ("reading", "cuts")
    # The hidden series stay listed: a chart you cannot undo is a chart broken.
    assert {s.key for s in timeline.series} >= {"reading", "cuts"}
    # The cut-marker caption goes with the markers.
    assert "split-point" not in timeline.subtitle.lower()
    # Every other chart is untouched.
    others = {c.id: c.title for c in charts if c.id != "system_window_fps"}
    assert others["throughput_by_cluster"] == "Throughput by cluster"


def test_hiding_every_series_is_ignored_rather_than_drawing_nothing(
    run_dir: Path, tmp_path: Path
) -> None:
    """An empty frame with a title is not a chart, and leaves no way back."""
    from app.reports.style import View

    views = {"cluster_window_fps": View(hidden=("Cluster 0", "Cluster 1"))}
    charts, _, _ = chartmod.render(
        parsemod.parse_tree(run_dir), tmp_path / "imgs",
        source_dir=run_dir, views=views,
    )
    assert any(c.id == "cluster_window_fps" for c in charts)


def test_a_view_survives_the_manifest_round_trip(run_dir: Path, tmp_path: Path) -> None:
    from app.reports.style import View

    views = {"map_summary": View(ylabel="Accuracy", hidden=("Overall",))}
    charts, _, _ = chartmod.render(
        parsemod.parse_tree(run_dir), tmp_path / "imgs",
        source_dir=run_dir, views=views,
    )
    payload = json.loads(json.dumps([c.to_dict() for c in charts]))
    stored = next(c for c in payload if c["id"] == "map_summary")

    assert stored["view"] == {"title": "", "xlabel": "", "ylabel": "Accuracy",
                             "hidden": ["Overall"]}
    assert View.from_dict(stored["view"]).hidden == ("Overall",)
    assert [s["key"] for s in stored["series"]] == ["Cluster 0", "Cluster 1", "Overall"]


def test_generic_charts_are_configurable_too(tmp_path: Path) -> None:
    """The schema-blind path offers its files as the series."""
    from app.reports.style import View

    source = tmp_path / "unknown"
    source.mkdir()
    for name, base in (("a.log", 12.0), ("b.log", 30.0)):
        (source / name).write_text(
            "\n".join(f"lat_ms={base + i % 5}" for i in range(30)), encoding="utf-8"
        )

    charts, _, _ = chartmod.render(parsemod.parse_tree(source), tmp_path / "imgs")
    trend = next(c for c in charts if c.id == "trend_lat_ms")
    assert [s.key for s in trend.series] == ["a.log", "b.log"]

    charts, _, _ = chartmod.render(
        parsemod.parse_tree(source), tmp_path / "imgs2",
        views={"trend_lat_ms": View(title="Only A", hidden=("b.log",))},
    )
    trend = next(c for c in charts if c.id == "trend_lat_ms")
    assert trend.title == "Only A"
    assert "1 file(s)" in trend.summary


def test_an_unknown_directory_still_falls_back_to_the_parser(tmp_path: Path) -> None:
    source = tmp_path / "unknown"
    source.mkdir()
    (source / "edge.log").write_text(
        "\n".join(f"edge_ms={12 + (i % 7) * 0.3:.2f}" for i in range(40)), encoding="utf-8"
    )

    charts, _, _ = render(source, tmp_path / "imgs")
    assert charts and charts[0].id.startswith("trend_")


def test_files_reporting_the_same_constant_are_not_a_comparison(tmp_path: Path) -> None:
    """Two logs both printing `clusters=2` are one fact, not two alternatives."""
    source = tmp_path / "constants"
    source.mkdir()
    for name in ("a.log", "b.log"):
        (source / name).write_text("clusters=2\n", encoding="utf-8")

    charts, _, _ = render(source, tmp_path / "imgs")
    assert [c.id for c in charts if c.id in ("comparison", "delta")] == []


# ------------------------------------------------------- the analysis window
def test_the_window_keeps_the_batches_an_operator_asked_for() -> None:
    """5 and 90 over 100 batches means batches 5 to 90 — inclusive, both ends."""
    kept = list(range(*Window(5, 90).bounds(100)))

    assert kept[0] == 4 and kept[-1] == 89       # 0-based: the 5th and the 90th
    assert len(kept) == 86
    assert Window(5, 90).clip(list(range(1, 101)))[:1] == [5]
    assert Window(5, 90).clip(list(range(1, 101)))[-1:] == [90]


def test_the_whole_run_is_the_default_and_costs_nothing() -> None:
    rows = list(range(40))
    assert Window().whole and Window().label == "whole run"
    assert Window().clip(rows) == rows
    assert Window().bounds(40) == (0, 40)


def test_a_window_too_narrow_to_select_anything_still_yields_a_reading() -> None:
    """An empty series is a chart that vanishes with nothing to say why."""
    for count in (1, 2, 3, 7):
        first, stop = Window(49, 51).bounds(count)
        assert stop > first, count


def test_a_nonsense_window_falls_back_to_the_whole_run() -> None:
    """Second line behind the API's validator, for manifests it never saw."""
    assert Window.of(90, 5).whole
    assert Window.of(-10, 400) == Window(0.0, 100.0)
    assert Window.of("x", None).whole                       # type: ignore[arg-type]
    assert Window.from_dict({"start": 5, "end": 90}) == Window(5.0, 90.0)
    assert Window.from_dict(None).whole


def test_the_window_is_one_span_of_the_system_clock(run_dir: Path) -> None:
    """Every series is judged against the same two moments, not its own length.

    Cutting each cluster at its own 20% mark would hand them different
    stretches of the run, and then comparing the clusters compares two
    different experiments.
    """
    whole = runlog.read_run(run_dir)
    part = runlog.read_run(run_dir, Window(20, 80))
    low, high = part.window_span

    assert (low, high) == pytest.approx((0.2 * whole.span_s, 0.8 * whole.span_s))
    assert all(low <= p.at <= high for p in part.system_fps)
    for points in part.cluster_fps.values():
        assert all(low <= p.at <= high for p in points)
    # …and every reading of the whole run inside the span is still here.
    assert [p.at for p in part.system_fps] == [
        p.at for p in whole.system_fps if low <= p.at <= high
    ]
    for cluster, points in part.cluster_fps.items():
        assert [p.at for p in points] == [
            p.at for p in whole.cluster_fps[cluster] if low <= p.at <= high
        ]


#: A run whose summary line and whose event stream are the same measurement,
#: which the shared fixture above deliberately is not. 100 batches 100 ms
#: apart is a 9.9 s clock, and 32 frames each makes `steady_fps` exactly
#: 99 * 32 / 9.9 = 320 -- a number the recompute has to land on.
def write_consistent_run(root: Path, batches: int = 100, step_ms: int = 100) -> Path:
    base, step = 1785127877009606331, step_ms * 1_000_000
    span = (batches - 1) * step_ms / 1000
    return write_run(root, name="results_0729_2204_split", **{
        "batch_done_ns.log": "".join(
            f"{base + i * step}" + (f" {32 / (step_ms / 1000):.2f}" if i >= 15 else "")
            + "\n" for i in range(batches)
        ),
        "fps_cluster_ns.log": "".join(
            f"{base + i * step} cluster=intermediate_queue_0 done={i + 1}"
            + (f" window_fps={32 / (step_ms / 1000):.2f}" if i >= 15 else "") + "\n"
            for i in range(batches)
        ),
        "fps_cluster.log":
            f"{base} cluster=intermediate_queue_0 fps=300.0 "
            f"steady_fps={(batches - 1) * 32 / span:.3f} done={batches} "
            f"frames={batches * 32} share=100.0%\n"
            f"{base} SYSTEM fps=300.0 done={batches} frames={batches * 32} clusters=1\n",
        "cut_change_ns.log": "",
        "map_window.log": "", "map.log": "",
    })


def test_the_whole_span_recompute_reproduces_the_runs_own_steady_fps(
    tmp_path: Path,
) -> None:
    """The check that the recipe *is* the run's: same events, same answer.

    `steady_fps` is every completion after the first over the time since the
    first, which is precisely a half-open window across the whole clock. If
    these two disagree, the windowed throughput is measuring something the run
    never claimed to.
    """
    data = runlog.read_run(write_consistent_run(tmp_path))
    stated = data.throughput["Cluster 0"]["steady_fps"]
    assert stated == pytest.approx(320.0)

    runlog.recompute(data, (0.0, data.span_s))
    assert data.throughput["Cluster 0"]["fps"] == pytest.approx(stated)
    assert data.throughput["Cluster 0"]["done"] == 99


def test_a_window_recounts_over_its_own_stretch(tmp_path: Path) -> None:
    """20-80% of a 9.9 s run is 5.94 s, and holds 60 of the 100 batches."""
    data = runlog.read_run(write_consistent_run(tmp_path), Window(20, 80))
    low, high = data.window_span

    assert (low, high) == pytest.approx((1.98, 7.92))
    assert data.throughput["Cluster 0"]["done"] == 60
    assert data.throughput["Cluster 0"]["fps"] == pytest.approx(60 * 32 / 5.94)
    assert data.system("done") == 60


def test_throughput_is_recounted_from_the_batches_inside_the_window(
    run_dir: Path,
) -> None:
    part = runlog.read_run(run_dir, Window(20, 80))
    low, high = part.window_span
    size = part.batch_size

    for cluster in part.clusters:
        done = len(part.cluster_batches[cluster])
        assert part.throughput[cluster]["done"] == done
        assert part.throughput[cluster]["frames"] == done * size
        assert part.throughput[cluster]["fps"] == pytest.approx(
            done * size / (high - low)
        )
    # The shares still add up over the clusters that remain.
    assert sum(part.throughput[c]["share"] for c in part.clusters) == pytest.approx(100.0)
    # Steady state is what a window already is; a second bar meaning the same
    # thing would be a comparison that is not in the data.
    assert "steady_fps" not in part.throughput["Cluster 0"]
    assert "throughput" in part.recomputed


def test_the_window_leaves_alone_what_the_run_only_totalled(run_dir: Path) -> None:
    """No per-batch series was written for these, so nothing can re-derive them."""
    part = runlog.read_run(run_dir, Window(20, 80))

    assert part.utilization[("System", "all")]["utilization"] == 53.20
    assert part.devices[0]["busy_s"] == 176.760
    assert part.latency[("System", "all", "e2e")]["n"] == 504
    assert part.accuracy[("Overall", "ALL")]["mAP50"] == 0.1634


def test_windowed_accuracy_is_the_mean_of_the_windows_left_inside(
    run_dir: Path,
) -> None:
    """`map.log` calls WINDOW "mean of N window(s)" — so recompute it that way."""
    part = runlog.read_run(run_dir, Window(0, 60))
    rows = part.accuracy_window["Cluster 0"]

    assert 0 < len(rows) < 10
    assert part.accuracy[("Cluster 0", "WINDOW")]["mAP50"] == pytest.approx(
        sum(r["mAP50"] for r in rows) / len(rows)
    )
    assert "accuracy_window" in part.recomputed


def test_a_window_past_where_a_run_scored_says_so_rather_than_going_quiet(
    run_dir: Path,
) -> None:
    """mAP covers whichever batches had ground truth, which can be the start."""
    part = runlog.read_run(run_dir, Window(90, 100))

    assert part.accuracy_window["Cluster 0"] == []
    assert ("Cluster 0", "WINDOW") not in part.accuracy      # never a stale figure
    assert any("mAP scoring window" in w and "none are left" in w
               for w in part.warnings)


def test_a_cut_outside_the_window_is_dropped_rather_than_drawn_at_the_edge(
    tmp_path: Path,
) -> None:
    """A split-point change is an event, so it is cut on the clock.

    Kept, its marker would be drawn at whichever edge of the axes it fell past
    and read as having happened inside the window.
    """
    # The run spans 1.28 s; put the change at 1.0 s, so it is inside the last
    # half of the clock and outside the first.
    late = f"{1785127877009606331 + 1_000_000_000} intermediate_queue_0: cut 11->12 deeper\n"
    directory = write_run(tmp_path, **{"cut_change_ns.log": late})

    assert len(runlog.read_run(directory).cuts) == 1
    assert len(runlog.read_run(directory, Window(50, 100)).cuts) == 1
    assert runlog.read_run(directory, Window(0, 50)).cuts == []


def test_every_chart_says_which_run_its_numbers_describe(
    run_dir: Path, tmp_path: Path
) -> None:
    """Both ways round: an unmarked chart would be read as windowed too."""
    charts, tiles, notes = chartmod.render(
        parsemod.parse_tree(run_dir), tmp_path / "imgs",
        source_dir=run_dir, window=Window(5, 90),
    )
    by_id = {c.id: c for c in charts}

    # Recounted from the batch events inside the span.
    assert "recomputed over 5–90% of the run" in by_id["throughput_by_cluster"].subtitle
    # Series, filtered to the same span.
    assert "5–90% of the run" in by_id["system_window_fps"].subtitle
    assert "5–90% of the run" in by_id["cluster_window_fps"].subtitle
    # Only a finished total was written, so it stays whole-run and says why.
    for chart_id in ("e2e_latency_profile", "utilization_by_role",
                     "device_utilization", "service_latency_by_role"):
        assert "whole run" in by_id[chart_id].subtitle, chart_id
    # One panel each way round gets its own sentence.
    assert "matched frame by frame" in by_id["map_summary"].subtitle

    by_label = {t.label: t for t in tiles}
    assert by_label["System throughput"].source.endswith("· 5–90%")
    assert by_label["System utilization"].source.endswith("· whole run")
    assert by_label["End-to-end latency"].source.endswith("· whole run")
    assert by_label["Batch size"].source == "config.yaml"

    assert any("one span for every chart" in n for n in notes)
    assert any("still whole-run" in n for n in notes)


def test_the_accuracy_summary_only_claims_a_windowed_mean_when_it_has_one(
    run_dir: Path, tmp_path: Path
) -> None:
    """A label has to follow what is on the chart, not what was intended.

    mAP scores whichever batches carried ground truth. A window holding none
    of them leaves the all-frames figure alone on the chart, and calling that
    "recomputed" would be the exact misreading the labels exist to stop.
    """
    charts, _, _ = chartmod.render(
        parsemod.parse_tree(run_dir), tmp_path / "imgs",
        source_dir=run_dir, window=Window(95, 100),
    )
    summary = next(c for c in charts if c.id == "map_summary")

    assert "recomputed" not in summary.subtitle
    assert "whole run" in summary.subtitle


def test_two_windows_draw_the_same_line_where_they_overlap(tmp_path: Path) -> None:
    """Same readings, same instant, same curve — whatever else was included.

    The rolling mean is sized from the whole run rather than from the slice on
    screen. Sized from the slice, 5-75% and 25-75% smoothed over different
    widths and disagreed at the shared right-hand end, where the underlying
    readings are identical — and the endpoint label disagreed with them.
    """
    from app.reports.style import rolling_mean, stable_smooth

    directory = write_consistent_run(tmp_path, batches=400, step_ms=50)
    ends = set()
    for start in (0.0001, 5, 25, 40):
        data = runlog.read_run(directory, Window(start, 75))
        values = np.array([p.value for p in data.system_fps])
        width = stable_smooth(len(values), data.full_counts.get("system", 0))
        smoothed = rolling_mean(values, width) if width else values
        ends.add((width, round(float(smoothed[-1]), 9),
                  round(data.system_fps[-1].at, 9)))

    assert len(ends) == 1, ends


def test_a_run_with_no_clock_refuses_the_window_instead_of_guessing(
    tmp_path: Path,
) -> None:
    """Without batch timestamps there is nothing to measure a span against."""
    directory = write_run(tmp_path, **{"batch_done_ns.log": "", "fps_cluster_ns.log": ""})

    data = runlog.read_run(directory, Window(5, 90))
    assert data.window.whole                      # not applied
    assert any("no clock" in w for w in data.warnings)
    assert data.throughput["Cluster 0"]["fps"] == 18.131      # untouched


def test_the_generic_path_windows_its_metrics_too(tmp_path: Path) -> None:
    source = tmp_path / "unknown"
    source.mkdir()
    (source / "edge.log").write_text(
        "\n".join(f"edge_ms={i}" for i in range(100)), encoding="utf-8"
    )

    parsed = parsemod.parse_tree(source)
    assert parsed.metrics["edge_ms"].count == 100
    charts, _, notes = chartmod.render(parsed, tmp_path / "imgs", window=Window(5, 90))

    # Readings 5..90 of the log, so the trend describes 86 of them.
    assert parsed.metrics["edge_ms"].values == [float(v) for v in range(4, 90)]
    assert any("86 readings" in c.summary for c in charts)
    assert any("5–90% window" in n for n in notes)


def test_a_windowed_run_still_reads_a_single_stated_value(tmp_path: Path) -> None:
    """One reading per file is a summary, not a series — a window keeps it."""
    source = tmp_path / "unknown"
    source.mkdir()
    (source / "edge.log").write_text(
        "\n".join(f"edge_ms={i}" for i in range(60)) + "\nPeak memory 1284 MB\n",
        encoding="utf-8",
    )

    _, tiles, _ = chartmod.render(
        parsemod.parse_tree(source), tmp_path / "imgs", window=Window(90, 95)
    )
    assert [t.label for t in tiles] == ["Peak memory"]
