"""空間/ボクセルヒートマップのE2Eテスト（Issue #54 / E9-5）。

`mock_twin_server`（tests/conftest.py）を上流に見立て、
1. 空間ジオメトリがあるモデル: `space-voxelize`で生成した`space_voxels.json`を
   `serve`が焼き込み、Liveトグル(E9-4)で空間ボクセルの色が集計値に応じて
   変わることを確認する。
2. 空間ジオメトリが無いモデル: `mapping.json`のspaceGuidバインディングが
   Storey GUIDを指す場合の、Storey配下要素メッシュへのフォールバック着色
   （digital-twin-spec.md §5.4）を確認する。
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


def _build_ifc_with_wall_and_space(tmp_path) -> tuple[Path, str, str]:
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
    return path, wall.GlobalId, space.GlobalId


def _storey_guid(usda_path: Path) -> str:
    stage = Usd.Stage.Open(str(usda_path))
    for prim in stage.Traverse():
        cd = prim.GetCustomData()
        if cd.get("class") == "IfcBuildingStorey":
            return cd["GUID"]
    raise AssertionError("storey not found")


@pytest.fixture(scope="module")
def served_url_with_space_voxels(tmp_path_factory, mock_twin_server):
    tmp_path = tmp_path_factory.mktemp("space_heatmap_e2e")
    ifc_path, _wall_guid, space_guid = _build_ifc_with_wall_and_space(tmp_path)
    usda = tmp_path / "model.usda"
    convert(ifc_path, usda)

    space_voxels_out = tmp_path / "space_voxels.json"
    exit_code = main(
        ["space-voxelize", str(ifc_path), "--reference", str(usda), "--size", "1.0", "-o", str(space_voxels_out)]
    )
    assert exit_code == 0
    space_voxels_json = json.loads(space_voxels_out.read_text())

    bindings = [{"pointId": "point-co2-1", "metric": "co2", "target": {"spaceGuid": space_guid}}]
    twin_json = build_twin_json(
        metrics=[{"name": "co2", "unit": "ppm", "colormap": "turbo", "min": 0, "max": 512}],
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

    yield f"http://127.0.0.1:{port}/", space_guid

    server.shutdown()
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def served_url_storey_fallback(tmp_path_factory, mock_twin_server):
    """空間ジオメトリを持たないモデル（既存のminimal.ifc）で、
    mapping.jsonのspaceGuidバインディングがStorey GUIDを指すフォールバック構成。"""
    tmp_path = tmp_path_factory.mktemp("space_heatmap_fallback_e2e")
    usda = tmp_path / "minimal.usda"
    convert(FIXTURE, usda)

    storey_guid = _storey_guid(usda)

    bindings = [{"pointId": "point-co2-1", "metric": "co2", "target": {"spaceGuid": storey_guid}}]
    twin_json = build_twin_json(
        metrics=[{"name": "co2", "unit": "ppm", "colormap": "turbo", "min": 0, "max": 512}],
        bindings=bindings,
        poll_interval_seconds=3600,
        stale_threshold_seconds=10**9,
    )

    workdir = tmp_path / "www"
    workdir.mkdir()
    build_serve_directory(usda, workdir, twin=twin_json)  # space_voxelsは渡さない

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


def test_space_voxel_heatmap_hidden_until_live_enabled(page, served_url_with_space_voxels):
    """Live有効化前は中間グレーの初期色のまま（実際の集計値でまだ着色されていない）。"""
    url, space_guid = served_url_with_space_voxels
    _wait_for_load(page, url)

    r, g, b = page.evaluate(f"window.ifc2usdViewer.getSpaceVoxelInstanceColor({space_guid!r})")
    assert abs(r - g) < 0.01 and abs(g - b) < 0.01, f"expected neutral gray, got rgb=({r},{g},{b})"


def test_space_voxel_heatmap_colors_space_by_aggregated_value(page, served_url_with_space_voxels):
    """value(512)==max(512)としたのでt=1.0、turbo(1.0)は赤が支配的な色になる
    （E9-4のtest_enabling_live_colors_bound_element_and_shows_legendと同じ根拠）。"""
    url, space_guid = served_url_with_space_voxels
    _wait_for_load(page, url)

    page.locator("#live-toggle").check()
    page.wait_for_function("window.ifc2usdViewer.isLiveEnabled() === true")
    page.wait_for_timeout(300)

    color = page.evaluate(f"window.ifc2usdViewer.getSpaceVoxelInstanceColor({space_guid!r})")
    assert color is not None
    r, g, b = color
    assert r > g and r > b, f"expected red-dominant heatmap color, got rgb=({r},{g},{b})"


def test_space_voxel_heatmap_hides_again_when_live_disabled(page, served_url_with_space_voxels):
    url, space_guid = served_url_with_space_voxels
    _wait_for_load(page, url)

    page.locator("#live-toggle").check()
    page.wait_for_function("window.ifc2usdViewer.isLiveEnabled() === true")
    page.wait_for_timeout(300)
    page.locator("#live-toggle").uncheck()

    lod_visible = page.evaluate("""
        () => window.ifc2usdViewer.getSpaceVoxelLods().every((lod) => lod.mesh.visible === false)
    """)
    assert lod_visible is True


def test_space_voxel_heatmap_respects_mesh_only_display_mode(page, served_url_with_space_voxels):
    """コードレビューで検出: displayModeが"mesh"（ボクセル非表示）のときは、
    Liveが有効でも空間ヒートマップのボクセルを表示しない
    （通常のボクセルレイヤーと同じ表示モード規約に揃える）。"""
    url, space_guid = served_url_with_space_voxels
    _wait_for_load(page, url)

    page.evaluate('window.ifc2usdViewer.setDisplayMode("mesh")')
    page.locator("#live-toggle").check()
    page.wait_for_function("window.ifc2usdViewer.isLiveEnabled() === true")
    page.wait_for_timeout(300)

    lod_hidden = page.evaluate("""
        () => window.ifc2usdViewer.getSpaceVoxelLods().every((lod) => lod.mesh.visible === false)
    """)
    assert lod_hidden is True

    page.evaluate('window.ifc2usdViewer.setDisplayMode("both")')
    lod_visible = page.evaluate("""
        () => window.ifc2usdViewer.getSpaceVoxelLods().some((lod) => lod.mesh.visible === true)
    """)
    assert lod_visible is True


def test_storey_fallback_colors_descendant_wall_elements(page, served_url_storey_fallback):
    """空間ジオメトリが無いモデルでも、mapping.jsonのspaceGuidバインディングが
    Storey GUIDを指していれば、そのStorey配下の要素メッシュがE9-4のオブジェクト
    色マッピング経路で着色される（digital-twin-spec.md §5.4のフォールバック）。"""
    _wait_for_load(page, served_url_storey_fallback)
    wall_guid = _guid_by_name(page, "Wall North")

    page.locator("#live-toggle").check()
    page.wait_for_function("window.ifc2usdViewer.isLiveEnabled() === true")
    page.wait_for_timeout(300)

    r, g, b = _sample_mesh_pixel_at_guid(page, wall_guid)
    assert r > g and r > b, f"expected red-dominant fallback color, got rgb=({r},{g},{b})"
