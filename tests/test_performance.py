from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import sys

import pytest

from ifc2usd import cli
from ifc2usd import performance
from ifc2usd.performance import (
    METRICS_SCHEMA_VERSION,
    ProcessMeasurement,
    comparison_markdown,
    run_benchmark,
    validate_metrics,
)


def _metrics() -> dict:
    return {
        "schemaVersion": METRICS_SCHEMA_VERSION,
        "generatedAt": "2026-08-26T00:00:00+00:00",
        "cachePolicy": {"processes": "cold", "browser": "cold-new-browser"},
        "environment": {
            "platform": "test-platform",
            "python": "3.11.0",
            "ifc2usd": "0.2.0",
        },
        "source": {"path": "minimal.ifc", "sizeBytes": 1234},
        "operations": {
            name: {"seconds": seconds, "peakRssBytes": 10_000_000}
            for name, seconds in {
                "convert": 1.0,
                "usdColdOpen": 0.2,
                "exportGltf": 0.5,
                "voxelize": 0.8,
            }.items()
        },
        "artifacts": {
            name: {"path": path, "sizeBytes": size}
            for name, path, size in (
                ("usd", "minimal.usdc", 1000),
                ("glb", "minimal.glb", 2000),
                ("voxelJson", "minimal_voxels.json", 3000),
                ("voxelUsd", "minimal_voxels.usda", 4000),
            )
        },
        "viewer": {
            "firstMeshSeconds": 0.4,
            "allAssetsSeconds": 0.7,
            "frameTimeMs": {"p50": 16.5, "p95": 19.2, "samples": 120},
            "render": {"drawCalls": 2, "triangles": 24, "geometries": 2},
        },
    }


def test_metrics_schema_accepts_complete_report():
    validate_metrics(_metrics())


@pytest.mark.parametrize(
    ("path", "message"),
    [
        (("cachePolicy", "processes"), "cachePolicy.processes"),
        (("operations", "convert", "peakRssBytes"), "operations.convert.peakRssBytes"),
        (("viewer", "frameTimeMs", "p95"), "viewer.frameTimeMs.p95"),
    ],
)
def test_metrics_schema_rejects_missing_required_values(path, message):
    metrics = deepcopy(_metrics())
    parent = metrics
    for key in path[:-1]:
        parent = parent[key]
    del parent[path[-1]]

    with pytest.raises(ValueError, match=message):
        validate_metrics(metrics)


def test_comparison_markdown_reports_baseline_and_percent_delta():
    baseline = _metrics()
    current = deepcopy(baseline)
    current["operations"]["convert"]["seconds"] = 0.75
    current["artifacts"]["glb"]["sizeBytes"] = 1500

    summary = comparison_markdown(current, baseline)

    assert "Convert time" in summary
    assert "-25.0%" in summary
    assert "GLB size" in summary


def test_current_summary_explains_how_to_supply_a_baseline():
    summary = performance.current_summary_markdown(_metrics())

    assert "No baseline was supplied" in summary
    assert "Convert time" in summary


def test_measure_process_uses_child_process_and_records_peak_rss(tmp_path):
    pid_path = tmp_path / "pid.txt"
    result = performance.measure_process(
        [
            sys.executable,
            "-c",
            (
                "from pathlib import Path; import os, time; "
                f"Path({str(pid_path)!r}).write_text(str(os.getpid())); "
                "allocation = bytearray(64 * 1024 * 1024); time.sleep(0.1)"
            ),
        ]
    )

    assert int(pid_path.read_text()) != os.getpid()
    assert result.seconds >= 0
    assert result.peak_rss_bytes >= 64 * 1024 * 1024


def test_viewer_metrics_preserve_long_frame_stalls():
    metrics = performance.summarize_viewer_metrics(
        first_mesh=1.0,
        all_assets=2.0,
        frame_times=[16.0] * 113 + [1500.0] * 7,
        render={"drawCalls": 10, "triangles": 20, "geometries": 5},
    )

    assert metrics["frameTimeMs"]["p95"] == 1500.0
    assert metrics["frameTimeMs"]["samples"] == 120


def test_run_benchmark_writes_metrics_and_comparison(monkeypatch, tmp_path):
    source = tmp_path / "minimal.ifc"
    source.write_bytes(b"IFC fixture")
    report_dir = tmp_path / "report"
    stale_artifact = report_dir / "artifacts" / "stale.txt"
    stale_artifact.parent.mkdir(parents=True)
    stale_artifact.write_text("stale", encoding="utf-8")
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps(_metrics()), encoding="utf-8")
    commands: list[list[str]] = []

    def fake_measure(command: list[str]) -> ProcessMeasurement:
        commands.append(command)
        if "convert" in command or "export-gltf" in command:
            Path(command[command.index("-o") + 1]).write_bytes(b"artifact")
        elif "voxelize" in command:
            output_base = Path(command[command.index("-o") + 1])
            output_base.with_suffix(".json").write_text("{}", encoding="utf-8")
            output_base.with_suffix(".usda").write_bytes(b"voxels")
        return ProcessMeasurement(seconds=0.1, peak_rss_bytes=1024)

    monkeypatch.setattr(performance, "measure_process", fake_measure)
    monkeypatch.setattr(
        performance,
        "measure_viewer",
        lambda *_args, **_kwargs: {
            "firstMeshSeconds": 0.2,
            "allAssetsSeconds": 0.3,
            "frameTimeMs": {"p50": 16.0, "p95": 18.0, "samples": 120},
            "render": {"drawCalls": 2, "triangles": 24, "geometries": 2},
        },
    )

    metrics = run_benchmark(source, report_dir, baseline_path=baseline_path)

    validate_metrics(metrics)
    assert len(commands) == 4
    assert len({tuple(command) for command in commands}) == 4
    assert json.loads((report_dir / "metrics.json").read_text(encoding="utf-8")) == metrics
    assert "Performance comparison" in (report_dir / "comparison.md").read_text(encoding="utf-8")
    assert not stale_artifact.exists()
    assert metrics["cachePolicy"] == {
        "processes": "cold",
        "browser": "cold-new-browser",
    }


def test_benchmark_cli_forwards_paths_sizes_and_baseline(monkeypatch, tmp_path):
    source = tmp_path / "minimal.ifc"
    source.write_text("fixture", encoding="utf-8")
    report_dir = tmp_path / "report"
    baseline = tmp_path / "baseline.json"
    baseline.write_text("{}", encoding="utf-8")
    captured: dict = {}

    def fake_run(ifc_path, output_path, *, voxel_sizes, baseline_path):
        captured.update(
            ifc_path=ifc_path,
            output_path=output_path,
            voxel_sizes=voxel_sizes,
            baseline_path=baseline_path,
        )
        return _metrics()

    monkeypatch.setattr(cli, "run_benchmark", fake_run, raising=False)

    exit_code = cli.main(
        [
            "benchmark",
            str(source),
            "-o",
            str(report_dir),
            "--size",
            "2.0",
            "--size",
            "1.0",
            "--baseline",
            str(baseline),
        ]
    )

    assert exit_code == 0
    assert captured == {
        "ifc_path": source,
        "output_path": report_dir,
        "voxel_sizes": (2.0, 1.0),
        "baseline_path": baseline,
    }