"""Playback: 時系列再生のE2Eテスト（Issue #55 / E9-6）。

`test_twin_live_e2e.py`と同じ`mock_twin_server`/`served_url_with_twin`パターンを
再利用する。`TWIN_HISTORY["point-temp-1"]`は2点（08:00=22.9, 09:00=23.4）固定
なので、Loadボタン押下後は必ずフレーム数2・スライダーmax=1になる。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import numpy as np
import pytest
from playwright.sync_api import sync_playwright
from pxr import Usd

import ifcopenshell
import ifcopenshell.api.aggregate
import ifcopenshell.api.context
import ifcopenshell.api.geometry
import ifcopenshell.api.project
import ifcopenshell.api.root
import ifcopenshell.api.spatial
import ifcopenshell.api.unit
import ifcopenshell.util.shape_builder

from ifc2usd import convert
from ifc2usd.cli import main
from ifc2usd.serve import build_serve_directory, make_server
from ifc2usd.twin import TwinClient, build_twin_json
from ifc2usd.twin_proxy import TwinProxy
from tests.conftest import chromium_launch_kwargs

FIXTURE = Path(__file__).parent / "fixtures" / "minimal.ifc"


def _build_ifc_with_wall_and_space(tmp_path) -> tuple[Path, str]:
    """test_space_heatmap_e2e.py の同名ヘルパーと同じ最小限の壁+空間IFC。"""
    model = ifcopenshell.api.project.create_file(version="IFC4")
    project = ifcopenshell.api.root.create_entity(model, ifc_class="IfcProject", name="P")
    metre = ifcopenshell.api.unit.add_si_unit(model, unit_type="LENGTHUNIT")
    ifcopenshell.api.unit.assign_unit(model, units=[metre])

    context = ifcopenshell.api.context.add_context(model, context_type="Model")
    body = ifcopenshell.api.context.add_context(
        model, context_type="Model", context_identifier="Body", target_view="MODEL_VIEW", parent=context
    )

    site = ifcopenshell.api.root.create_entity(model, ifc_class="IfcSite", name="Site")
    building = ifcopenshell.api.root.create_entity(model, ifc_class="IfcBuilding", name="Building")
    storey = ifcopenshell.api.root.create_entity(model, ifc_class="IfcBuildingStorey", name="Storey")
    ifcopenshell.api.aggregate.assign_object(model, products=[site], relating_object=project)
    ifcopenshell.api.aggregate.assign_object(model, products=[building], relating_object=site)
    ifcopenshell.api.aggregate.assign_object(model, products=[storey], relating_object=building)

    builder = ifcopenshell.util.shape_builder.ShapeBuilder(model)

    wall = ifcopenshell.api.root.create_entity(model, ifc_class="IfcWall", name="Wall")
    wall_profile = builder.rectangle(size=np.array([4.0, 0.2]))
    wall_solid = builder.extrude(wall_profile, magnitude=3.0, position=np.array([0.0, 0.0, 0.0]))
    wall_representation = builder.get_representation(body, [wall_solid])
    ifcopenshell.api.geometry.assign_representation(model, product=wall, representation=wall_representation)
    ifcopenshell.api.spatial.assign_container(model, products=[wall], relating_structure=storey)

    space = ifcopenshell.api.root.create_entity(model, ifc_class="IfcSpace", name="Room 101")
    space_profile = builder.rectangle(size=np.array([4.0, 3.0]))
    space_solid = builder.extrude(space_profile, magnitude=3.0, position=np.array([0.0, 0.0, 0.0]))
    space_representation = builder.get_representation(body, [space_solid])
    ifcopenshell.api.geometry.assign_representation(model, product=space, representation=space_representation)
    ifcopenshell.api.aggregate.assign_object(model, products=[space], relating_object=storey)

    path = tmp_path / "wall_and_space.ifc"
    model.write(str(path))
    return path, space.GlobalId


def _wall_guid(usda_path: Path, name: str) -> str:
    stage = Usd.Stage.Open(str(usda_path))
    for prim in stage.Traverse():
        cd = prim.GetCustomData()
        if cd.get("class") == "IfcWall" and cd.get("Name") == name:
            return cd["GUID"]
    raise AssertionError(f"wall not found: {name}")


@pytest.fixture(scope="module")
def north_wall_guid(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("twin_playback_e2e_guid")
    usda = tmp_path / "minimal.usda"
    convert(FIXTURE, usda)
    return _wall_guid(usda, "Wall North")


@pytest.fixture(scope="module")
def served_url_with_twin(tmp_path_factory, mock_twin_server, north_wall_guid):
    tmp_path = tmp_path_factory.mktemp("twin_playback_e2e")
    usda = tmp_path / "minimal.usda"
    convert(FIXTURE, usda)

    bindings = [
        {"pointId": "point-temp-1", "metric": "temperature", "target": {"guid": north_wall_guid}}
    ]
    # min/maxはあえて指定せず、フレームごとの値からP5/P95で自動決定させる
    # （viewer.js側の自動決定ロジックそのものを検証するため）。
    twin_json = build_twin_json(
        metrics=[{"name": "temperature", "unit": "celsius", "colormap": "turbo"}],
        bindings=bindings,
        poll_interval_seconds=3600,
        stale_threshold_seconds=10**9,
    )

    workdir = tmp_path / "www"
    workdir.mkdir()
    build_serve_directory(usda, workdir, twin=twin_json)

    client = TwinClient(mock_twin_server)
    proxy = TwinProxy(client, bindings, ttl_seconds=twin_json["pollIntervalSeconds"])

    server = make_server(workdir, port=0, twin_proxy=proxy)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    yield f"http://127.0.0.1:{port}/"

    server.shutdown()
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def served_url_without_twin(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("twin_playback_e2e_no_twin")
    usda = tmp_path / "minimal.usda"
    convert(FIXTURE, usda)

    workdir = tmp_path / "www"
    workdir.mkdir()
    build_serve_directory(usda, workdir)

    server = make_server(workdir, port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    yield f"http://127.0.0.1:{port}/"

    server.shutdown()
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def served_url_with_space_voxels_and_twin(tmp_path_factory, mock_twin_server):
    """空間ボクセルヒートマップ(E9-5)がある構成でのPlayback。pointIdは既存の
    `point-temp-1`（TWIN_HISTORYに2点のデータがある）をそのままspaceGuid
    バインディングへ流用する（conftest.pyのTWIN_HISTORYにco2用の履歴データが
    無いため、metric名だけ変えて同じ既存データを再利用する）。"""
    tmp_path = tmp_path_factory.mktemp("twin_playback_space_voxels_e2e")
    ifc_path, space_guid = _build_ifc_with_wall_and_space(tmp_path)
    usda = tmp_path / "model.usda"
    convert(ifc_path, usda)

    space_voxels_out = tmp_path / "space_voxels.json"
    exit_code = main(
        ["space-voxelize", str(ifc_path), "--reference", str(usda), "--size", "1.0", "-o", str(space_voxels_out)]
    )
    assert exit_code == 0
    space_voxels_json = json.loads(space_voxels_out.read_text())

    bindings = [{"pointId": "point-temp-1", "metric": "temperature", "target": {"spaceGuid": space_guid}}]
    twin_json = build_twin_json(
        metrics=[{"name": "temperature", "unit": "celsius", "colormap": "turbo"}],
        bindings=bindings,
        poll_interval_seconds=3600,
        stale_threshold_seconds=10**9,
    )

    workdir = tmp_path / "www"
    workdir.mkdir()
    build_serve_directory(usda, workdir, twin=twin_json, space_voxels=space_voxels_json)

    client = TwinClient(mock_twin_server)
    proxy = TwinProxy(client, bindings, ttl_seconds=twin_json["pollIntervalSeconds"])
    server = make_server(workdir, port=0, twin_proxy=proxy)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    yield f"http://127.0.0.1:{port}/"

    server.shutdown()
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(**chromium_launch_kwargs())
        yield b
        b.close()


@pytest.fixture
def page(browser):
    p = browser.new_page(viewport={"width": 1000, "height": 700})
    yield p
    p.close()


def _wait_for_load(page, url, e2e: bool = True):
    page.goto(f"{url}?e2e" if e2e else url)
    page.wait_for_function("window.ifc2usdLoaded === true", timeout=10000)


def _guid_by_name(page, name):
    return page.evaluate(f"""
        () => {{
            let found = null;
            window.ifc2usdViewer.modelRoot.traverse((obj) => {{
                if (obj.userData && obj.userData.name === {name!r}) found = obj.userData.guid;
            }});
            return found;
        }}
    """)


_CANDIDATE_VIEW_DIRECTIONS = [
    (5, 10, 7),
    (1, 1, 1),
    (1, 1, -1),
    (1, -1, 1),
    (-1, 1, 1),
    (1, 0, 0),
    (0, 1, 0),
    (0, 0, 1),
    (-1, -1, -1),
    (0, 1, 1),
]


def _sample_mesh_pixel_at_guid(page, guid):
    page.evaluate('window.ifc2usdViewer.setDisplayMode("mesh")')
    best = (32, 32, 32)
    best_saturation = -1
    for dx, dy, dz in _CANDIDATE_VIEW_DIRECTIONS:
        page.evaluate(f"""
            () => {{
                window.ifc2usdViewer.camera.position.set({dx}, {dy}, {dz});
                window.ifc2usdViewer.controls.target.set(0, 0, 0);
                const box = window.ifc2usdViewer.getBoundingBoxOfGuid("{guid}");
                window.ifc2usdViewer.fitCameraToBox(box, {{ paddingFactor: 1.5 }});
            }}
        """)
        page.wait_for_timeout(100)
        r, g, b = page.evaluate("""
            () => {
                const canvas = document.querySelector('#viewport canvas');
                const tmp = document.createElement('canvas');
                tmp.width = canvas.width;
                tmp.height = canvas.height;
                const ctx = tmp.getContext('2d');
                ctx.drawImage(canvas, 0, 0);
                const x = Math.floor(tmp.width / 2);
                const y = Math.floor(tmp.height / 2);
                const [r, g, b] = ctx.getImageData(x, y, 1, 1).data;
                return [r, g, b];
            }
        """)
        saturation = max(r, g, b) - min(r, g, b)
        if saturation > best_saturation:
            best_saturation = saturation
            best = (r, g, b)
    return best


def test_playback_toolbar_group_hidden_without_twin_asset(page, served_url_without_twin):
    _wait_for_load(page, served_url_without_twin)
    assert page.locator("#playback-toolbar-group").is_visible() is False


def test_playback_toolbar_group_visible_with_twin_asset(page, served_url_with_twin):
    _wait_for_load(page, served_url_with_twin)
    assert page.locator("#playback-toolbar-group").is_visible() is True
    assert page.locator("#playback-slider").is_disabled() is True
    assert page.locator("#playback-play-toggle").is_disabled() is True


def test_load_populates_frames_and_colors_first_frame(page, served_url_with_twin, north_wall_guid):
    """TWIN_HISTORY["point-temp-1"]は08:00=22.9, 09:00=23.4の2点固定なので、
    Load後はスライダーmax=1になり、先頭フレーム適用でWall North本来の
    displayColor(赤系0.8,0.2,0.2)とは異なる色に塗り替わるはず。"""
    _wait_for_load(page, served_url_with_twin)

    original_rgb = _sample_mesh_pixel_at_guid(page, north_wall_guid)

    page.locator("#playback-load-button").click()
    page.wait_for_function("!document.querySelector('#playback-slider').disabled", timeout=10000)

    assert page.locator("#playback-slider").get_attribute("max") == "1"
    assert page.locator("#playback-play-toggle").is_disabled() is False
    assert page.locator("#live-legend").is_visible() is True

    frame0_rgb = _sample_mesh_pixel_at_guid(page, north_wall_guid)
    assert frame0_rgb != original_rgb, (
        f"expected playback frame 0 to recolor the element, still original rgb={original_rgb}"
    )


def test_scrubbing_slider_changes_color_between_frames(page, served_url_with_twin, north_wall_guid):
    _wait_for_load(page, served_url_with_twin)

    page.locator("#playback-load-button").click()
    page.wait_for_function("!document.querySelector('#playback-slider').disabled", timeout=10000)

    frame0_rgb = _sample_mesh_pixel_at_guid(page, north_wall_guid)

    page.evaluate("""
        () => {
            const slider = document.querySelector('#playback-slider');
            slider.value = '1';
            slider.dispatchEvent(new Event('input'));
        }
    """)
    page.wait_for_timeout(100)

    frame1_rgb = _sample_mesh_pixel_at_guid(page, north_wall_guid)
    assert frame1_rgb != frame0_rgb, "scrubbing to a different frame should change the applied color"
    assert page.locator("#playback-time-label").inner_text() == "2026-07-08T09:00:00Z"


def test_enabling_live_while_playing_stops_playback_conflict(page, served_url_with_twin, north_wall_guid):
    """再生読み込み開始時にLiveがOFFへ強制されること（loadTwin()のコメントにある
    「再生読み込み中はLiveポーリングと色の書き込みが競合しないよう止める」）を検証する。"""
    _wait_for_load(page, served_url_with_twin)

    page.locator("#live-toggle").check()
    page.wait_for_function("window.ifc2usdViewer.isLiveEnabled() === true")
    page.wait_for_timeout(300)

    page.locator("#playback-load-button").click()
    page.wait_for_function("!document.querySelector('#playback-slider').disabled", timeout=10000)

    assert page.evaluate("window.ifc2usdViewer.isLiveEnabled()") is False
    assert page.locator("#live-toggle").is_checked() is False


def test_enabling_live_while_autoplaying_stops_the_playback_timer(page, served_url_with_twin):
    """コードレビューで検出: Playback再生中(setInterval稼働中)にLiveを有効化しても
    再生タイマーが止まらず、Live/Playbackの両方が独立に色を書き込み合っていた
    （Play/PauseボタンのテキストがPlayに戻らないことで再生タイマー停止を確認する）。"""
    _wait_for_load(page, served_url_with_twin)

    page.locator("#playback-load-button").click()
    page.wait_for_function("!document.querySelector('#playback-slider').disabled", timeout=10000)

    page.locator("#playback-play-toggle").click()
    assert page.locator("#playback-play-toggle").inner_text() == "Pause"

    page.locator("#live-toggle").check()
    page.wait_for_function("window.ifc2usdViewer.isLiveEnabled() === true")

    assert page.locator("#playback-play-toggle").inner_text() == "Play"


def test_changing_metric_resets_stale_playback_frames(page, served_url_with_twin):
    """コードレビューで検出: 再生フレーム読み込み後にメトリックを切り替えても
    playbackFrames/playbackMin/Maxは旧メトリックのままで、凡例の単位だけが
    新メトリックへ切り替わる食い違いが起きていた。メトリック変更でスライダーが
    再び無効化される（=再読み込みが必要な状態にリセットされる）ことを確認する。"""
    _wait_for_load(page, served_url_with_twin)

    page.locator("#playback-load-button").click()
    page.wait_for_function("!document.querySelector('#playback-slider').disabled", timeout=10000)

    # このフィクスチャのtwin.jsonにはtemperature1つしか無いため、同じ値への
    # 再選択でもchangeイベントを発火させれば同じリセット経路を通る。
    page.evaluate("""
        () => {
            const select = document.querySelector('#live-metric-select');
            select.dispatchEvent(new Event('change'));
        }
    """)

    assert page.locator("#playback-slider").is_disabled() is True
    assert page.locator("#playback-play-toggle").is_disabled() is True


def test_space_voxel_heatmap_visible_during_playback(page, served_url_with_space_voxels_and_twin):
    """コードレビューで検出: 空間ボクセルヒートマップの表示条件がliveEnabledのみを
    見ており、Playback読み込み時にLiveが強制OFFされるため再生中は空間ヒートマップが
    常に非表示になっていた（値は計算・着色されるのに描画されない）。"""
    _wait_for_load(page, served_url_with_space_voxels_and_twin)

    assert page.evaluate("window.ifc2usdViewer.getSpaceVoxelLods().every((lod) => !lod.mesh.visible)") is True

    page.locator("#playback-load-button").click()
    page.wait_for_function("!document.querySelector('#playback-slider').disabled", timeout=10000)

    assert page.evaluate("window.ifc2usdViewer.getSpaceVoxelLods().some((lod) => lod.mesh.visible)") is True
