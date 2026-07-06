"""選択ハイライト強化のE2Eテスト（Issue #42 / E8-1）。

バックフェイス方式アウトライン(mesh/voxel)、ゴースト表示、ダブルクリックフィット、
および実装前に検証が必要だった「同色要素間でのマテリアル波及」を検証する。
"""

from __future__ import annotations

import threading
from pathlib import Path

import ifcopenshell
import ifcopenshell.api.aggregate
import ifcopenshell.api.context
import ifcopenshell.api.geometry
import ifcopenshell.api.project
import ifcopenshell.api.root
import ifcopenshell.api.spatial
import ifcopenshell.api.style
import ifcopenshell.api.unit
import ifcopenshell.util.shape_builder
import pytest
from playwright.sync_api import sync_playwright

from ifc2usd import convert
from ifc2usd.serve import build_serve_directory, make_server
from tests.conftest import chromium_launch_kwargs
from tests.generate_fixture import _add_wall

FIXTURE = Path(__file__).parent / "fixtures" / "minimal.ifc"


def _build_same_color_fixture(ifc_path: Path) -> None:
    """2枚の壁が全く同じdisplayColorを持つIFCを作る。gltf.pyが書き出すマテリアルは
    trimeshのGLB出力時に同一プロパティ値なら1つに重複排除されるため(検証済み)、
    このフィクスチャでのみ「同色要素間の意図しない波及」を検証できる。
    """
    model = ifcopenshell.api.project.create_file(version="IFC4")
    ifcopenshell.api.root.create_entity(model, ifc_class="IfcProject", name="P")
    metre = ifcopenshell.api.unit.add_si_unit(model, unit_type="LENGTHUNIT")
    ifcopenshell.api.unit.assign_unit(model, units=[metre])
    context = ifcopenshell.api.context.add_context(model, context_type="Model")
    body = ifcopenshell.api.context.add_context(
        model, context_type="Model", context_identifier="Body", target_view="MODEL_VIEW", parent=context
    )
    site = ifcopenshell.api.root.create_entity(model, ifc_class="IfcSite", name="S")
    building = ifcopenshell.api.root.create_entity(model, ifc_class="IfcBuilding", name="B")
    storey = ifcopenshell.api.root.create_entity(model, ifc_class="IfcBuildingStorey", name="F")
    project = model.by_type("IfcProject")[0]
    ifcopenshell.api.aggregate.assign_object(model, products=[site], relating_object=project)
    ifcopenshell.api.aggregate.assign_object(model, products=[building], relating_object=site)
    ifcopenshell.api.aggregate.assign_object(model, products=[storey], relating_object=building)
    builder = ifcopenshell.util.shape_builder.ShapeBuilder(model)

    same_color = (0.8, 0.2, 0.2)
    _add_wall(model, body, storey, builder, "Wall A", (0.0, 0.0, 0.0), (5.0, 0.2, 3.0), same_color)
    _add_wall(model, body, storey, builder, "Wall B", (5.0, 0.0, 0.0), (0.2, 4.0, 3.0), same_color)

    ifc_path.parent.mkdir(parents=True, exist_ok=True)
    model.write(str(ifc_path))


def _serve_fixture(tmp_path, fixture_path, name, voxel_sizes=(0.5,)):
    usda = tmp_path / f"{name}.usda"
    convert(fixture_path, usda)

    workdir = tmp_path / name
    workdir.mkdir()
    build_serve_directory(usda, workdir, voxel_sizes=voxel_sizes)

    server = make_server(workdir, port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{port}/"


@pytest.fixture(scope="module")
def served_url(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("selection_highlight_e2e")
    server, thread, url = _serve_fixture(tmp_path, FIXTURE, "www")
    yield url
    server.shutdown()
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def same_color_served_url(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("selection_highlight_same_color_e2e")
    ifc_path = tmp_path / "same_color.ifc"
    _build_same_color_fixture(ifc_path)
    server, thread, url = _serve_fixture(tmp_path, ifc_path, "www_same_color")
    yield url
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


def _wait_for_load(page, url):
    # preserveDrawingBuffer(画素検証に必要)は?e2eクエリで有効化する。
    page.goto(f"{url}?e2e")
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


def _mesh_emissive_hex(page, guid):
    return page.evaluate(f"""
        () => {{
            let hex = null;
            window.ifc2usdViewer.getGlbRoot().traverse((child) => {{
                if (child.isMesh && child.userData.guid === {guid!r}) {{
                    hex = child.material.emissive ? child.material.emissive.getHex() : null;
                }}
            }});
            return hex;
        }}
    """)


def _non_background_pixel_count(page):
    return page.evaluate("""
        () => {
            const canvas = document.querySelector('#viewport canvas');
            const tmp = document.createElement('canvas');
            tmp.width = canvas.width;
            tmp.height = canvas.height;
            const ctx = tmp.getContext('2d');
            ctx.drawImage(canvas, 0, 0);
            const { data } = ctx.getImageData(0, 0, tmp.width, tmp.height);
            const bg = [32, 32, 32];
            let count = 0;
            for (let i = 0; i < data.length; i += 4) {
                if (
                    Math.abs(data[i] - bg[0]) > 10 ||
                    Math.abs(data[i + 1] - bg[1]) > 10 ||
                    Math.abs(data[i + 2] - bg[2]) > 10
                ) {
                    count++;
                }
            }
            return count;
        }
    """)


def _outline_colored_pixel_count(page):
    """HIGHLIGHT_EMISSIVE(0x3355ff = rgb(51,85,255))に近い画素数を数える。
    アウトラインは選択メッシュ本体の背後にBackSideで描かれる薄い縁取りなので、
    単一ピクセルの厳密一致より「その色が画面上に一定量現れたか」の方が
    ヘッドレスレンダラ(SwiftShader)下で安定する。"""
    return page.evaluate("""
        () => {
            const canvas = document.querySelector('#viewport canvas');
            const tmp = document.createElement('canvas');
            tmp.width = canvas.width;
            tmp.height = canvas.height;
            const ctx = tmp.getContext('2d');
            ctx.drawImage(canvas, 0, 0);
            const { data } = ctx.getImageData(0, 0, tmp.width, tmp.height);
            const target = [51, 85, 255];
            let count = 0;
            for (let i = 0; i < data.length; i += 4) {
                if (
                    Math.abs(data[i] - target[0]) < 40 &&
                    Math.abs(data[i + 1] - target[1]) < 40 &&
                    Math.abs(data[i + 2] - target[2]) < 40
                ) {
                    count++;
                }
            }
            return count;
        }
    """)


def _fit_and_render(page, guid, padding_factor=1.8):
    page.evaluate(f"""
        () => {{
            const box = window.ifc2usdViewer.getBoundingBoxOfGuid({guid!r});
            window.ifc2usdViewer.camera.position.set(5, 10, 7);
            window.ifc2usdViewer.controls.target.set(0, 0, 0);
            window.ifc2usdViewer.fitCameraToBox(box, {{ paddingFactor: {padding_factor} }});
        }}
    """)
    page.wait_for_timeout(150)


# --- 事前検証: 共有マテリアルへの波及 (clone-on-write) ---------------------------


def test_highlighting_one_element_does_not_bleed_emissive_to_same_colored_element(page, same_color_served_url):
    """Wall A/Bは全く同じdisplayColorのため、gltf.pyが書き出すGLBではtrimeshにより
    1つのマテリアルへ重複排除される(検証済み)。Wall Aを選択してもWall Bのemissiveが
    変化しないこと(=クローンされ、共有マテリアルへ波及していないこと)を確認する。"""
    _wait_for_load(page, same_color_served_url)

    wall_a = _guid_by_name(page, "Wall A")
    wall_b = _guid_by_name(page, "Wall B")
    assert wall_a and wall_b and wall_a != wall_b

    before_a = _mesh_emissive_hex(page, wall_a)
    before_b = _mesh_emissive_hex(page, wall_b)
    assert before_a == before_b  # 選択前は同一(既定値)のはず

    page.evaluate(f"window.ifc2usdViewer.selectByGuid({wall_a!r})")

    after_a = _mesh_emissive_hex(page, wall_a)
    after_b = _mesh_emissive_hex(page, wall_b)
    assert after_a == 0x3355FF
    assert after_b == before_b  # Wall Bは変化していない(波及なし)


# --- アウトライン(バックフェイス・ハル) -----------------------------------------


def test_selecting_mesh_element_shows_outline_and_deselecting_hides_it(page, served_url):
    _wait_for_load(page, served_url)
    page.evaluate('window.ifc2usdViewer.setDisplayMode("mesh")')
    guid = _guid_by_name(page, "Wall North")

    _fit_and_render(page, guid)
    before = _outline_colored_pixel_count(page)

    page.evaluate(f"window.ifc2usdViewer.selectByGuid({guid!r})")
    page.wait_for_timeout(100)
    during = _outline_colored_pixel_count(page)

    page.evaluate("window.ifc2usdViewer.selectByGuid(null)")
    page.wait_for_timeout(100)
    after = _outline_colored_pixel_count(page)

    assert during > before
    assert after < during


def test_selecting_voxel_only_element_shows_outline(page, served_url):
    _wait_for_load(page, served_url)
    page.evaluate('window.ifc2usdViewer.setDisplayMode("voxel")')
    guid = _guid_by_name(page, "Wall North")

    _fit_and_render(page, guid)
    before = _outline_colored_pixel_count(page)

    page.evaluate(f"window.ifc2usdViewer.selectByGuid({guid!r})")
    page.wait_for_timeout(100)
    during = _outline_colored_pixel_count(page)

    assert during > before


def test_section_clip_also_clips_the_outline(page, served_url):
    """FR: 断面クリップ(renderer.clippingPlanes)がアウトラインにも正しく効くこと。
    renderer.clippingPlanesはグローバル設定のため追加対応は不要な想定だが、
    実際に画面から消えることまで確認する。"""
    _wait_for_load(page, served_url)
    page.evaluate('window.ifc2usdViewer.setDisplayMode("mesh")')
    guid = _guid_by_name(page, "Wall North")

    _fit_and_render(page, guid)
    page.evaluate(f"window.ifc2usdViewer.selectByGuid({guid!r})")
    page.wait_for_timeout(100)
    unclipped = _outline_colored_pixel_count(page)
    assert unclipped > 0

    box = page.evaluate("""
        () => {
            const box = window.ifc2usdViewer.getBoundingBox();
            return { min: box.min.y, max: box.max.y };
        }
    """)
    page.evaluate(f"window.ifc2usdViewer.setSectionClipHeight({box['min']})")
    page.wait_for_timeout(150)
    clipped = _outline_colored_pixel_count(page)

    assert clipped < unclipped


# --- ゴースト表示 ---------------------------------------------------------------


def test_ghost_mode_dims_non_selected_without_bleeding_shared_material(page, same_color_served_url):
    _wait_for_load(page, same_color_served_url)

    wall_a = _guid_by_name(page, "Wall A")
    wall_b = _guid_by_name(page, "Wall B")

    page.evaluate("window.ifc2usdViewer.setGhostModeEnabled(true)")
    page.evaluate(f"window.ifc2usdViewer.selectByGuid({wall_a!r})")

    is_a_ghosted = page.evaluate(f"""
        () => {{
            let ghosted = null;
            window.ifc2usdViewer.getGlbRoot().traverse((child) => {{
                if (child.isMesh && child.userData.guid === {wall_a!r}) {{
                    ghosted = window.ifc2usdViewer.isMeshGhosted(child);
                }}
            }});
            return ghosted;
        }}
    """)
    is_b_ghosted = page.evaluate(f"""
        () => {{
            let ghosted = null;
            window.ifc2usdViewer.getGlbRoot().traverse((child) => {{
                if (child.isMesh && child.userData.guid === {wall_b!r}) {{
                    ghosted = window.ifc2usdViewer.isMeshGhosted(child);
                }}
            }});
            return ghosted;
        }}
    """)

    assert is_a_ghosted is False  # 選択中の要素自体はゴーストされない
    assert is_b_ghosted is True


def test_reselecting_a_previously_ghosted_element_still_gets_highlighted(page, same_color_served_url):
    """コードレビュー指摘の回帰テスト: ゴーストモードがONのまま選択をAからBへ
    切り替えると、Bはこの瞬間まで非選択(ゴースト済み)だったため
    mesh.materialが共有の_ghostMaterial(MeshBasicMaterial、emissive無し)を
    指している。highlightMeshのemissiveガードがこれを見て静かにスキップして
    しまうと、Bは選択状態(輪郭・ツリー行・プロパティパネル)にはなるのに
    emissiveハイライトだけが付かない不整合が起きる。"""
    _wait_for_load(page, same_color_served_url)

    wall_a = _guid_by_name(page, "Wall A")
    wall_b = _guid_by_name(page, "Wall B")

    page.evaluate("window.ifc2usdViewer.setGhostModeEnabled(true)")
    page.evaluate(f"window.ifc2usdViewer.selectByGuid({wall_a!r})")  # Bはこの時点でゴースト済み
    page.evaluate(f"window.ifc2usdViewer.selectByGuid({wall_b!r})")  # AからBへ切り替え

    assert _mesh_emissive_hex(page, wall_b) == 0x3355FF

    # ゴーストOFFで元のマテリアルに復元される
    page.evaluate("window.ifc2usdViewer.setGhostModeEnabled(false)")
    is_b_ghosted_after = page.evaluate(f"""
        () => {{
            let ghosted = null;
            window.ifc2usdViewer.getGlbRoot().traverse((child) => {{
                if (child.isMesh && child.userData.guid === {wall_b!r}) {{
                    ghosted = window.ifc2usdViewer.isMeshGhosted(child);
                }}
            }});
            return ghosted;
        }}
    """)
    assert is_b_ghosted_after is False


def _center_pixel(page):
    return page.evaluate("""
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


def test_ghost_mode_visibly_dims_non_selected_element(page, served_url):
    """ゴーストは不透明度を下げるだけで footprint(被覆面積) 自体は変わらない
    ため、ピクセル数ではなく彩度(色の乗り具合)で検証する: 半透明の共有グレー
    マテリアルへの差し替えにより、選択されていないWall Eastの画面中央での
    色は元の彩度の高い色から、背景(0x202020、無彩色)寄りの低彩度な色へ変わる。"""
    _wait_for_load(page, served_url)
    page.evaluate('window.ifc2usdViewer.setDisplayMode("mesh")')

    wall_east = _guid_by_name(page, "Wall East")
    wall_north = _guid_by_name(page, "Wall North")

    _fit_and_render(page, wall_east, padding_factor=3.0)
    before_r, before_g, before_b = _center_pixel(page)
    before_saturation = max(before_r, before_g, before_b) - min(before_r, before_g, before_b)
    assert before_saturation > 20  # Wall Eastの彩度のある色が写っている前提

    page.evaluate("window.ifc2usdViewer.setGhostModeEnabled(true)")
    page.evaluate(f"window.ifc2usdViewer.selectByGuid({wall_north!r})")
    page.wait_for_timeout(100)
    after_r, after_g, after_b = _center_pixel(page)
    after_saturation = max(after_r, after_g, after_b) - min(after_r, after_g, after_b)

    assert after_saturation < before_saturation * 0.5


# --- ダブルクリックフィット -------------------------------------------------------


def test_double_click_tree_row_fits_camera_to_element(page, served_url):
    _wait_for_load(page, served_url)
    guid = _guid_by_name(page, "Wall East")

    # THREEはグローバル公開されていないため、box中心はJS側で直接計算する。
    expected_center = page.evaluate(f"""
        () => {{
            const box = window.ifc2usdViewer.getBoundingBoxOfGuid({guid!r});
            return [(box.min.x + box.max.x) / 2, (box.min.y + box.max.y) / 2, (box.min.z + box.max.z) / 2];
        }}
    """)

    page.locator(f'li[data-guid="{guid}"] > .tree-label').dblclick()

    new_target = page.evaluate("window.ifc2usdViewer.controls.target.toArray()")
    for actual, expected in zip(new_target, expected_center):
        assert actual == pytest.approx(expected, abs=1e-3)
    assert page.evaluate("window.ifc2usdViewer.getSelectedGuid()") == guid


def test_double_click_on_3d_element_selects_and_refits_camera(page, served_url):
    _wait_for_load(page, served_url)
    page.evaluate('window.ifc2usdViewer.setDisplayMode("mesh")')
    guid = _guid_by_name(page, "Wall North")

    # dblclickハンドラ自身はfitCameraToBoxをオプション無し(既定paddingFactor)で
    # 呼ぶため、比較基準の初回フィットも同じ既定値を使う(_fit_and_renderの
    # padding_factor=1.8は別の目的の既定値であり、ここで使うと距離が食い違う)。
    page.evaluate(f"""
        () => {{
            const box = window.ifc2usdViewer.getBoundingBoxOfGuid({guid!r});
            window.ifc2usdViewer.camera.position.set(5, 10, 7);
            window.ifc2usdViewer.controls.target.set(0, 0, 0);
            window.ifc2usdViewer.fitCameraToBox(box);
        }}
    """)
    page.wait_for_timeout(150)
    original_distance = page.evaluate("""
        () => window.ifc2usdViewer.camera.position.distanceTo(window.ifc2usdViewer.controls.target)
    """)

    # ズームインして距離を意図的に崩す(視線方向・注視点は変えないのでスクリーン上の
    # 位置は変わらない)。
    page.evaluate("""
        () => {
            const v = window.ifc2usdViewer;
            v.camera.position.lerp(v.controls.target, 0.9);
        }
    """)
    zoomed_distance = page.evaluate("""
        () => window.ifc2usdViewer.camera.position.distanceTo(window.ifc2usdViewer.controls.target)
    """)
    assert zoomed_distance < original_distance * 0.5

    rect = page.evaluate("""
        () => {
            const canvas = document.querySelector('#viewport canvas');
            const r = canvas.getBoundingClientRect();
            return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
        }
    """)
    page.mouse.dblclick(rect["x"], rect["y"])

    assert page.evaluate("window.ifc2usdViewer.getSelectedGuid()") == guid
    new_distance = page.evaluate("""
        () => window.ifc2usdViewer.camera.position.distanceTo(window.ifc2usdViewer.controls.target)
    """)
    assert new_distance == pytest.approx(original_distance, rel=0.05)
