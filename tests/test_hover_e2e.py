"""ホバー連携のE2Eテスト（Issue #43 / E8-2）。"""

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
    tmp_path = tmp_path_factory.mktemp("hover_e2e")
    usda = tmp_path / "minimal.usda"
    convert(FIXTURE, usda)

    workdir = tmp_path / "www"
    workdir.mkdir()
    build_serve_directory(usda, workdir, voxel_sizes=(0.5,))

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


def _tree_row_has_class(page, guid, class_name):
    return page.evaluate(f"""
        () => {{
            const li = document.querySelector('li[data-guid="{guid}"]');
            return li ? li.classList.contains({class_name!r}) : null;
        }}
    """)


def _dispatch_pointer_move_at_canvas_center(page, buttons=0):
    page.evaluate(f"""
        () => {{
            const canvas = document.querySelector('#viewport canvas');
            const rect = canvas.getBoundingClientRect();
            canvas.dispatchEvent(new PointerEvent('pointermove', {{
                clientX: rect.left + rect.width / 2,
                clientY: rect.top + rect.height / 2,
                bubbles: true,
                isPrimary: true,
                buttons: {buttons},
            }}));
        }}
    """)


def _wait_one_animation_frame(page):
    # animate()のrAFループが実際に1回以上回ることを保証する（2重rAFで確実に
    # 次のフレーム境界をまたぐ）。
    page.evaluate("""
        () => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))
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


def test_hovering_3d_object_sets_pointer_cursor(page, served_url):
    """ux-spec.md §3.2: 3D側のホバーで「カーソルをpointerにする」。"""
    _wait_for_load(page, served_url)
    page.evaluate('window.ifc2usdViewer.setDisplayMode("mesh")')
    guid = _guid_by_name(page, "Wall North")
    _fit_and_render(page, guid)

    canvas_cursor = lambda: page.evaluate("""
        () => document.querySelector('#viewport canvas').style.cursor
    """)
    assert canvas_cursor() != "pointer"

    _dispatch_pointer_move_at_canvas_center(page)
    _wait_one_animation_frame(page)
    assert canvas_cursor() == "pointer"

    # 何もない場所へ移動するとdefaultへ戻る。
    page.evaluate("""
        () => {
            const canvas = document.querySelector('#viewport canvas');
            canvas.dispatchEvent(new PointerEvent('pointermove', {
                clientX: 1, clientY: 1, bubbles: true, isPrimary: true, buttons: 0,
            }));
        }
    """)
    _wait_one_animation_frame(page)
    assert canvas_cursor() != "pointer"


def test_hovering_3d_object_adds_hovered_class_to_tree_row(page, served_url):
    _wait_for_load(page, served_url)
    page.evaluate('window.ifc2usdViewer.setDisplayMode("mesh")')
    guid = _guid_by_name(page, "Wall North")
    _fit_and_render(page, guid)

    assert _tree_row_has_class(page, guid, "hovered") is False

    _dispatch_pointer_move_at_canvas_center(page)
    _wait_one_animation_frame(page)

    assert page.evaluate("window.ifc2usdViewer.getHoverGuid()") == guid
    assert _tree_row_has_class(page, guid, "hovered") is True


def test_hovering_tree_row_applies_hover_tint_to_3d_object(page, served_url):
    _wait_for_load(page, served_url)
    guid = _guid_by_name(page, "Wall North")

    assert _mesh_emissive_hex(page, guid) == 0

    page.locator(f'li[data-guid="{guid}"] .tree-label').hover()

    assert page.evaluate("window.ifc2usdViewer.getHoverGuid()") == guid
    assert _mesh_emissive_hex(page, guid) == 0x222A44

    # ツリー行から離れると3D側のホバー表現も解除される(双方向)。
    page.mouse.move(1, 1)
    assert page.evaluate("window.ifc2usdViewer.getHoverGuid()") is None
    assert _mesh_emissive_hex(page, guid) == 0


def test_hover_raycast_runs_at_most_once_per_animation_frame(page, served_url):
    _wait_for_load(page, served_url)
    page.evaluate('window.ifc2usdViewer.setDisplayMode("mesh")')

    count_before = page.evaluate("window.ifc2usdViewer.getHoverRaycastCount()")

    # 1回のpage.evaluate内(=1つのJSタスク)で5回連続dispatchする。Python側から
    # 5回page.evaluateを呼ぶと呼び出し間でanimate()のrAFが実際に進んでしまい、
    # 「1フレームに複数回pointermoveが来ても1回に間引かれる」ことの検証になら
    # ない(各page.evaluate呼び出しの往復の間にフレームが挟まりうるため)。
    page.evaluate("""
        () => {
            const canvas = document.querySelector('#viewport canvas');
            const rect = canvas.getBoundingClientRect();
            for (let i = 0; i < 5; i++) {
                canvas.dispatchEvent(new PointerEvent('pointermove', {
                    clientX: rect.left + rect.width / 2,
                    clientY: rect.top + rect.height / 2,
                    bubbles: true,
                    isPrimary: true,
                    buttons: 0,
                }));
            }
        }
    """)

    _wait_one_animation_frame(page)
    count_after = page.evaluate("window.ifc2usdViewer.getHoverRaycastCount()")

    assert count_after - count_before == 1


def test_hover_is_skipped_while_dragging(page, served_url):
    """event.buttons!==0(ドラッグ操作中)のpointermoveはホバー更新自体をスキップする。"""
    _wait_for_load(page, served_url)
    page.evaluate('window.ifc2usdViewer.setDisplayMode("mesh")')
    guid = _guid_by_name(page, "Wall North")
    _fit_and_render(page, guid)

    _dispatch_pointer_move_at_canvas_center(page, buttons=1)
    _wait_one_animation_frame(page)

    assert page.evaluate("window.ifc2usdViewer.getHoverGuid()") is None


def test_hover_does_not_scroll_the_tree_panel(page, served_url):
    _wait_for_load(page, served_url)
    guid = _guid_by_name(page, "Wall East")

    page.evaluate("document.getElementById('tree-panel').scrollTop = 3")
    before = page.evaluate("document.getElementById('tree-panel').scrollTop")

    page.locator(f'li[data-guid="{guid}"] .tree-label').hover()

    after = page.evaluate("document.getElementById('tree-panel').scrollTop")
    assert after == before


def test_hovering_the_selected_element_does_not_override_its_highlight(page, served_url):
    _wait_for_load(page, served_url)
    guid = _guid_by_name(page, "Wall North")

    page.evaluate(f"window.ifc2usdViewer.selectByGuid({guid!r})")
    assert _mesh_emissive_hex(page, guid) == 0x3355FF

    page.locator(f'li[data-guid="{guid}"] .tree-label').hover()

    assert page.evaluate("window.ifc2usdViewer.getHoverGuid()") == guid
    assert _mesh_emissive_hex(page, guid) == 0x3355FF  # 選択表現のまま(ホバー色に上書きされない)
