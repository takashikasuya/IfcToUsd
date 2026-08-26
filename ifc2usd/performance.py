"""Reproducible cold-process and cold-browser measurements.

Each CLI operation runs in a fresh process. Viewer measurements launch a new
Chromium process, discard the first animation-frame interval, and summarize the
next 120 intervals using nearest-rank percentiles.
"""

from __future__ import annotations

from collections.abc import Iterable
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import platform
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any

from . import __version__

METRICS_SCHEMA_VERSION = 1
_RSS_SAMPLE_INTERVAL_SECONDS = 0.25 if sys.platform == "win32" else 0.05


@dataclass(frozen=True)
class ProcessMeasurement:
    seconds: float
    peak_rss_bytes: int

_REQUIRED_PATHS = (
    "generatedAt",
    "cachePolicy.processes",
    "cachePolicy.browser",
    "environment.platform",
    "environment.python",
    "environment.ifc2usd",
    "source.path",
    "source.sizeBytes",
    "operations.convert.seconds",
    "operations.convert.peakRssBytes",
    "operations.usdColdOpen.seconds",
    "operations.usdColdOpen.peakRssBytes",
    "operations.exportGltf.seconds",
    "operations.exportGltf.peakRssBytes",
    "operations.voxelize.seconds",
    "operations.voxelize.peakRssBytes",
    "artifacts.usd.path",
    "artifacts.usd.sizeBytes",
    "artifacts.glb.path",
    "artifacts.glb.sizeBytes",
    "artifacts.voxelJson.path",
    "artifacts.voxelJson.sizeBytes",
    "artifacts.voxelUsd.path",
    "artifacts.voxelUsd.sizeBytes",
    "viewer.firstMeshSeconds",
    "viewer.allAssetsSeconds",
    "viewer.frameTimeMs.p50",
    "viewer.frameTimeMs.p95",
    "viewer.frameTimeMs.samples",
    "viewer.render.drawCalls",
    "viewer.render.triangles",
    "viewer.render.geometries",
)


def _value_at(data: dict[str, Any], path: str) -> Any:
    value: Any = data
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            raise ValueError(f"missing required metrics value: {path}")
        value = value[part]
    return value


def validate_metrics(metrics: dict[str, Any]) -> None:
    """Raise ``ValueError`` when a metrics document violates schema v1."""
    if metrics.get("schemaVersion") != METRICS_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported metrics schemaVersion: {metrics.get('schemaVersion')!r}"
        )
    for path in _REQUIRED_PATHS:
        _value_at(metrics, path)


def _comparison_rows() -> Iterable[tuple[str, str, str]]:
    yield "Convert time", "operations.convert.seconds", "s"
    yield "Voxelize time", "operations.voxelize.seconds", "s"
    yield "USD cold open", "operations.usdColdOpen.seconds", "s"
    yield "Peak convert RSS", "operations.convert.peakRssBytes", "bytes"
    yield "USD size", "artifacts.usd.sizeBytes", "bytes"
    yield "GLB size", "artifacts.glb.sizeBytes", "bytes"
    yield "First mesh display", "viewer.firstMeshSeconds", "s"
    yield "All assets ready", "viewer.allAssetsSeconds", "s"
    yield "Draw calls", "viewer.render.drawCalls", "count"
    yield "Frame time p95", "viewer.frameTimeMs.p95", "ms"


def _format_value(value: float, unit: str) -> str:
    if unit == "bytes":
        return f"{value / 1024 / 1024:.2f} MiB"
    if unit == "count":
        return f"{value:,.0f}"
    return f"{value:.3f} {unit}"


def comparison_markdown(current: dict[str, Any], baseline: dict[str, Any]) -> str:
    """Build a stable human-readable comparison between two valid reports."""
    validate_metrics(current)
    validate_metrics(baseline)
    lines = [
        "# Performance comparison",
        "",
        "| Metric | Baseline | Current | Delta |",
        "| --- | ---: | ---: | ---: |",
    ]
    for label, path, unit in _comparison_rows():
        old = float(_value_at(baseline, path))
        new = float(_value_at(current, path))
        delta = "n/a" if old == 0 else f"{(new - old) / old * 100:+.1f}%"
        lines.append(
            f"| {label} | {_format_value(old, unit)} | {_format_value(new, unit)} | {delta} |"
        )
    _append_voxel_phases(lines, current)
    return "\n".join(lines) + "\n"


def _append_voxel_phases(lines: list[str], metrics: dict[str, Any]) -> None:
    phases = metrics.get("operations", {}).get("voxelize", {}).get("phases")
    if not isinstance(phases, dict):
        return
    rows = [
        ("USD open", phases["usdOpenSeconds"]),
        ("Element extraction", phases["elementsFromStageSeconds"]),
        *[
            (f"Occupancy {size} m", value)
            for size, value in phases["occupancyBySizeSeconds"].items()
        ],
        ("JSON build", phases["jsonBuildSeconds"]),
        ("JSON write", phases["jsonWriteSeconds"]),
        ("PointInstancer", phases["pointInstancerBuildSeconds"]),
    ]
    lines.extend(["", "## Current voxel phases", "", "| Phase | Time |", "| --- | ---: |"])
    lines.extend(f"| {name} | {value:.3f} s |" for name, value in rows)


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


class _ProcessEntry32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


def _windows_process_tree_pids(root_pid: int) -> set[int]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32))
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32))
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot == wintypes.HANDLE(-1).value:
        return {root_pid}
    parents: dict[int, int] = {}
    try:
        entry = _ProcessEntry32(dwSize=ctypes.sizeof(_ProcessEntry32))
        if kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            while True:
                parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                    break
    finally:
        kernel32.CloseHandle(snapshot)

    tree = {root_pid}
    while True:
        children = {pid for pid, parent in parents.items() if parent in tree}
        expanded = tree | children
        if expanded == tree:
            return tree
        tree = expanded


def _windows_process_rss_bytes(pid: int) -> int:
    counters = _ProcessMemoryCounters(ctypes.sizeof(_ProcessMemoryCounters))
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    psapi.GetProcessMemoryInfo.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(_ProcessMemoryCounters),
        wintypes.DWORD,
    )
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(0x0410, False, pid)
    if not handle:
        return 0
    try:
        if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            return int(counters.WorkingSetSize)
        return 0
    finally:
        kernel32.CloseHandle(handle)


def _process_rss_bytes(process: subprocess.Popen[str]) -> int:
    if sys.platform == "win32":
        return sum(
            _windows_process_rss_bytes(pid)
            for pid in _windows_process_tree_pids(process.pid)
        )

    status_path = Path(f"/proc/{process.pid}/status")
    if status_path.is_file():
        for line in status_path.read_text(encoding="ascii").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024

    try:
        completed = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(process.pid)],
            check=True,
            capture_output=True,
            text=True,
        )
        return int(completed.stdout.strip()) * 1024
    except (FileNotFoundError, subprocess.SubprocessError, ValueError):
        return 0


def measure_process(command: list[str], *, cwd: Path | None = None) -> ProcessMeasurement:
    """Run one cold child process and capture elapsed time plus peak RSS."""
    started = time.perf_counter()
    peak_rss = 0
    with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as output:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdout=output,
            stderr=subprocess.STDOUT,
            text=True,
        )
        while process.poll() is None:
            try:
                peak_rss = max(peak_rss, _process_rss_bytes(process))
            except OSError:
                pass
            time.sleep(_RSS_SAMPLE_INTERVAL_SECONDS)

        elapsed = time.perf_counter() - started
        if process.returncode:
            output.seek(0)
            details = output.read()[-4000:]
            raise RuntimeError(
                f"benchmark command failed ({process.returncode}): {' '.join(command)}\n{details}"
            )

    if peak_rss <= 0:
        raise RuntimeError(f"could not measure peak RSS: {' '.join(command)}")
    return ProcessMeasurement(seconds=elapsed, peak_rss_bytes=peak_rss)


def _operation(measurement: ProcessMeasurement) -> dict[str, float | int]:
    return {
        "seconds": round(measurement.seconds, 6),
        "peakRssBytes": measurement.peak_rss_bytes,
    }


def _artifact(path: Path) -> dict[str, str | int]:
    if not path.is_file():
        raise FileNotFoundError(f"expected benchmark artifact not found: {path}")
    return {"path": str(path), "sizeBytes": path.stat().st_size}


def _load_voxel_profile(path: Path) -> dict[str, Any]:
    profile = json.loads(path.read_text(encoding="utf-8"))
    if profile.get("version") != 1:
        raise ValueError(f"unsupported voxel profile version: {profile.get('version')!r}")
    phases = profile.get("phases")
    required = {
        "usdOpenSeconds",
        "elementsFromStageSeconds",
        "occupancyBySizeSeconds",
        "jsonBuildSeconds",
        "jsonWriteSeconds",
        "pointInstancerBuildSeconds",
    }
    if not isinstance(phases, dict) or set(phases) != required:
        raise ValueError(f"malformed voxel profile phases: {path}")

    def valid_timing(value: Any) -> bool:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and value >= 0
        )

    if not valid_timing(profile.get("totalSeconds")):
        raise ValueError(f"malformed voxel profile totalSeconds: {path}")
    occupancy = phases["occupancyBySizeSeconds"]
    scalar_values = [value for name, value in phases.items() if name != "occupancyBySizeSeconds"]
    if not isinstance(occupancy, dict) or not all(
        valid_timing(value)
        for value in [*scalar_values, *occupancy.values(), profile["totalSeconds"]]
    ):
        raise ValueError(f"malformed voxel profile timings: {path}")
    return profile


def measure_viewer(
    usd_path: Path,
    workdir: Path,
    *,
    voxel_sizes: tuple[float, ...],
) -> dict[str, Any]:
    """Measure a cold viewer load and animation frames in a new Chromium process."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "viewer measurement requires the dev dependency: install with 'uv sync'"
        ) from exc

    from .serve import build_serve_directory, make_server

    workdir.mkdir(parents=True, exist_ok=True)
    build_serve_directory(usd_path, workdir, voxel_sizes=voxel_sizes)
    server = make_server(workdir, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"

    try:
        with sync_playwright() as playwright:
            browser = None
            try:
                browser = playwright.chromium.launch(
                    args=["--use-gl=swiftshader", "--enable-webgl", "--ignore-gpu-blocklist"]
                )
                page = browser.new_page(viewport={"width": 1600, "height": 950})
                started = time.perf_counter()
                page.goto(url)
                page.wait_for_function("window.ifc2usdMeshLoaded === true", timeout=180_000)
                first_mesh = time.perf_counter() - started
                page.wait_for_function("window.ifc2usdLoaded === true", timeout=180_000)
                all_assets = time.perf_counter() - started
                frame_times = page.evaluate(
                    """() => new Promise((resolve) => {
                        const samples = [];
                        let previous = performance.now();
                        const tick = (now) => {
                            samples.push(now - previous);
                            previous = now;
                            if (samples.length < 121) requestAnimationFrame(tick);
                            else resolve(samples.slice(1));
                        };
                        requestAnimationFrame(tick);
                    })"""
                )
                render = page.evaluate(
                    """() => {
                        const info = window.ifc2usdViewer.renderer.info;
                        return {
                            drawCalls: info.render.calls,
                            triangles: info.render.triangles,
                            geometries: info.memory.geometries,
                        };
                    }"""
                )
            finally:
                if browser is not None:
                    browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        if thread.is_alive():
            raise RuntimeError("benchmark HTTP server did not stop cleanly")

    return summarize_viewer_metrics(
        first_mesh=first_mesh,
        all_assets=all_assets,
        frame_times=frame_times,
        render=render,
    )


def summarize_viewer_metrics(
    *,
    first_mesh: float,
    all_assets: float,
    frame_times: list[float],
    render: dict[str, int],
) -> dict[str, Any]:
    """Validate and summarize browser observations without hiding long stalls."""
    ordered = sorted(float(value) for value in frame_times)
    if len(ordered) < 100 or any(value <= 0 or not math.isfinite(value) for value in ordered):
        raise RuntimeError("viewer returned invalid animation frame measurements")
    if any(not isinstance(render.get(key), int) or render[key] < 0 for key in (
        "drawCalls", "triangles", "geometries"
    )):
        raise RuntimeError("viewer returned invalid render statistics")

    def percentile(fraction: float) -> float:
        index = max(0, int(len(ordered) * fraction + 0.999999) - 1)
        return round(ordered[index], 3)

    return {
        "firstMeshSeconds": round(first_mesh, 6),
        "allAssetsSeconds": round(all_assets, 6),
        "frameTimeMs": {
            "p50": percentile(0.50),
            "p95": percentile(0.95),
            "samples": len(ordered),
        },
        "render": render,
    }


def current_summary_markdown(metrics: dict[str, Any]) -> str:
    lines = [
        "# Performance comparison",
        "",
        "No baseline was supplied. Re-run with `--baseline <metrics.json>` for deltas.",
        "",
        "| Metric | Current |",
        "| --- | ---: |",
    ]
    for label, path, unit in _comparison_rows():
        lines.append(f"| {label} | {_format_value(float(_value_at(metrics, path)), unit)} |")
    _append_voxel_phases(lines, metrics)
    return "\n".join(lines) + "\n"


def run_benchmark(
    ifc_path: Path,
    report_dir: Path,
    *,
    voxel_sizes: tuple[float, ...] = (0.5,),
    baseline_path: Path | None = None,
) -> dict[str, Any]:
    """Run the full cold benchmark and write metrics plus a comparison summary."""
    if not ifc_path.is_file():
        raise FileNotFoundError(ifc_path)

    baseline = None
    if baseline_path is not None:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        validate_metrics(baseline)

    report_dir.mkdir(parents=True, exist_ok=True)
    progress_path = report_dir / "progress.json"
    for report_path in (report_dir / "metrics.json", report_dir / "comparison.md", progress_path):
        report_path.unlink(missing_ok=True)
    artifacts_dir = report_dir / "artifacts"
    viewer_dir = report_dir / "viewer"
    for generated_dir in (artifacts_dir, viewer_dir):
        if generated_dir.exists():
            shutil.rmtree(generated_dir)
        generated_dir.mkdir()

    usd_path = artifacts_dir / f"{ifc_path.stem}.usdc"
    glb_path = artifacts_dir / f"{ifc_path.stem}.glb"
    voxel_base = artifacts_dir / f"{ifc_path.stem}_voxels"
    voxel_json_path = voxel_base.with_suffix(".json")
    voxel_usd_path = voxel_base.with_suffix(".usda")
    voxel_profile_path = artifacts_dir / f"{ifc_path.stem}_voxel_profile.json"
    executable = sys.executable
    completed_operations: dict[str, dict[str, float | int]] = {}

    def checkpoint(name: str, measurement: ProcessMeasurement) -> None:
        completed_operations[name] = _operation(measurement)
        progress_path.write_text(
            json.dumps({"operations": completed_operations}, indent=2) + "\n",
            encoding="utf-8",
        )

    convert = measure_process(
        [executable, "-m", "ifc2usd", "convert", str(ifc_path), "-o", str(usd_path)]
    )
    checkpoint("convert", convert)
    usd_open = measure_process(
        [
            executable,
            "-c",
            (
                "from pxr import Usd; import sys; "
                "stage = Usd.Stage.Open(sys.argv[1]); "
                "assert stage is not None and stage.GetDefaultPrim()"
            ),
            str(usd_path),
        ]
    )
    checkpoint("usdColdOpen", usd_open)
    export_gltf = measure_process(
        [executable, "-m", "ifc2usd", "export-gltf", str(usd_path), "-o", str(glb_path)]
    )
    checkpoint("exportGltf", export_gltf)
    voxel_command = [
        executable,
        "-m",
        "ifc2usd",
        "voxelize",
        str(usd_path),
        "-o",
        str(voxel_base),
        "--profile",
        str(voxel_profile_path),
    ]
    for size in voxel_sizes:
        voxel_command.extend(("--size", str(size)))
    voxelize = measure_process(voxel_command)
    checkpoint("voxelize", voxelize)
    voxel_profile = _load_voxel_profile(voxel_profile_path)
    viewer = measure_viewer(
        usd_path,
        viewer_dir,
        voxel_sizes=voxel_sizes,
    )

    metrics = {
        "schemaVersion": METRICS_SCHEMA_VERSION,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "cachePolicy": {
            "processes": "cold",
            "browser": "cold-new-browser",
            "rssSampleIntervalSeconds": _RSS_SAMPLE_INTERVAL_SECONDS,
        },
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "ifc2usd": __version__,
        },
        "source": _artifact(ifc_path),
        "operations": {
            "convert": _operation(convert),
            "usdColdOpen": _operation(usd_open),
            "exportGltf": _operation(export_gltf),
            "voxelize": {
                **_operation(voxelize),
                "phases": voxel_profile["phases"],
                "profiledTotalSeconds": voxel_profile["totalSeconds"],
            },
        },
        "artifacts": {
            "usd": _artifact(usd_path),
            "glb": _artifact(glb_path),
            "voxelJson": _artifact(voxel_json_path),
            "voxelUsd": _artifact(voxel_usd_path),
            "voxelProfile": _artifact(voxel_profile_path),
        },
        "viewer": viewer,
    }
    validate_metrics(metrics)
    (report_dir / "metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    if baseline is None:
        summary = current_summary_markdown(metrics)
    else:
        summary = comparison_markdown(metrics, baseline)
    (report_dir / "comparison.md").write_text(summary, encoding="utf-8")
    progress_path.unlink(missing_ok=True)
    return metrics