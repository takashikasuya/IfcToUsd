"""ボクセル→GUID逆引き選択のE2Eテスト（Issue #16 / E3-8）。"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from ifc2usd import convert
from ifc2usd.serve import build_serve_directory, make_server
from tests.conftest import chromium_launch_kwargs

FIXTURE = Path(__file__).parent / "fixtures" / "minimal.ifc"


@pytest.fixture(scope="module")
def served_url(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("voxel_selection_e2e")
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
    page.goto(url)
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


def _click_center_of(page, guid):
    page.evaluate(f"""
        () => {{
            const box = window.ifc2usdViewer.getBoundingBoxOfGuid("{guid}");
            window.ifc2usdViewer.fitCameraToBox(box, {{ paddingFactor: 1.5 }});
        }}
    """)
    viewport_box = page.locator("#viewport").bounding_box()
    cx = viewport_box["x"] + viewport_box["width"] / 2
    cy = viewport_box["y"] + viewport_box["height"] / 2
    page.mouse.click(cx, cy)


def test_clicking_voxel_in_voxel_mode_selects_a_valid_element(page, served_url):
    """ボクセルモードでクリックすると、(occlusionにより意図した要素と別の場合は
    あるが)必ずどれかの実在要素GUIDが選択され、nullのままにはならない。"""
    _wait_for_load(page, served_url)

    page.check('input[name="display-mode"][value="voxel"]')

    wall_guid = _guid_by_name(page, "Wall North")
    _click_center_of(page, wall_guid)

    selected = page.evaluate("window.ifc2usdViewer.getSelectedGuid()")
    assert selected is not None

    known_guids = page.evaluate("""
        () => {
            const guids = [];
            window.ifc2usdViewer.modelRoot.traverse((obj) => {
                if (obj.userData && obj.userData.guid) guids.push(obj.userData.guid);
            });
            return guids;
        }
    """)
    assert selected in known_guids


def test_raycast_targets_exclude_glb_root_in_voxel_mode(page, served_url):
    """回帰テスト: three.jsのRaycasterは祖先のvisibleを辿らず各ノード自身のvisible
    しか見ないため、glbRoot(GLTFLoaderのルートGroup)のvisible=falseだけでは
    その子メッシュ自身(visible=trueのまま)へのレイキャストを防げない。
    そのため表示モードに応じたレイキャスト対象は、visibleフラグに頼らず
    displayMode/activeVoxelLodIndexから明示的に組み立てる必要がある
    （currentRaycastTargets）。この構造自体を検証する。"""
    _wait_for_load(page, served_url)

    page.check('input[name="display-mode"][value="mesh"]')
    mesh_mode_targets = page.evaluate("""
        () => window.ifc2usdViewer.currentRaycastTargets().map(t => t.isInstancedMesh === true)
    """)
    assert mesh_mode_targets == [False]  # glbRootのみ、InstancedMeshは含まれない

    page.check('input[name="display-mode"][value="voxel"]')
    voxel_mode_targets = page.evaluate("""
        () => window.ifc2usdViewer.currentRaycastTargets().map(t => t.isInstancedMesh === true)
    """)
    assert voxel_mode_targets == [True]  # アクティブなvoxel LODのInstancedMeshのみ

    page.check('input[name="display-mode"][value="both"]')
    both_mode_targets = page.evaluate(
        "() => window.ifc2usdViewer.currentRaycastTargets().length"
    )
    assert both_mode_targets == 2


def test_voxel_click_syncs_tree_selection_and_property_panel(page, served_url):
    _wait_for_load(page, served_url)

    page.check('input[name="display-mode"][value="voxel"]')

    wall_guid = _guid_by_name(page, "Wall North")
    _click_center_of(page, wall_guid)
    selected = page.evaluate("window.ifc2usdViewer.getSelectedGuid()")

    is_tree_selected = page.eval_on_selector(
        f'li[data-guid="{selected}"]', "el => el.classList.contains('selected')"
    )
    assert is_tree_selected

    panel_text = page.locator("#property-panel").inner_text()
    assert selected in panel_text


def test_mesh_mode_click_is_unaffected_by_voxel_reverse_lookup(page, served_url):
    """メッシュモードではボクセルは不可視のためレイキャストに参加せず、
    Issue #13のクリック選択挙動が変わらないことを確認する（回帰防止）。"""
    _wait_for_load(page, served_url)

    page.check('input[name="display-mode"][value="mesh"]')

    wall_guid = _guid_by_name(page, "Wall North")
    _click_center_of(page, wall_guid)

    assert page.evaluate("window.ifc2usdViewer.getSelectedGuid()") == wall_guid


def test_voxel_mode_click_on_empty_space_does_not_select(page, served_url):
    _wait_for_load(page, served_url)

    page.check('input[name="display-mode"][value="voxel"]')

    # モデル全体を大きめの余白でズームアウトしてから隅をクリックする。
    # paddingFactorを既定(1.2)よりかなり大きくして、視界の隅に何も無いことを
    # 確実にする（隅クリック自体はモデル形状次第でわずかに揺れうるため）。
    page.evaluate("""
        () => window.ifc2usdViewer.fitCameraToBox(
            window.ifc2usdViewer.getBoundingBox(), { paddingFactor: 3.0 }
        )
    """)
    viewport_box = page.locator("#viewport").bounding_box()
    corner_x = viewport_box["x"] + 5
    corner_y = viewport_box["y"] + 5
    page.mouse.click(corner_x, corner_y)

    assert page.evaluate("window.ifc2usdViewer.getSelectedGuid()") is None
