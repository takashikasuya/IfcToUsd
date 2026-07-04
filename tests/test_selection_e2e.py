"""3Dクリック選択 + プロパティパネルのE2Eテスト（Issue #13 / E3-5）。"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from ifc2usd import convert
from ifc2usd.serve import build_serve_directory, make_server

FIXTURE = Path(__file__).parent / "fixtures" / "minimal.ifc"

_CHROMIUM_PATH = "/opt/pw-browsers/chromium"
_LAUNCH_ARGS = ["--use-gl=swiftshader", "--enable-webgl", "--ignore-gpu-blocklist"]


@pytest.fixture(scope="module")
def served_url(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("selection_e2e")
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
        b = p.chromium.launch(executable_path=_CHROMIUM_PATH, args=_LAUNCH_ARGS)
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
    """指定GUIDのオブジェクトが画面中央を占めるようカメラをフィットし、中央をクリックする。"""
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


def test_clicking_3d_object_selects_and_highlights(page, served_url):
    _wait_for_load(page, served_url)

    wall_guid = _guid_by_name(page, "Wall North")
    assert wall_guid

    assert page.evaluate("window.ifc2usdViewer.getSelectedGuid()") is None

    _click_center_of(page, wall_guid)

    assert page.evaluate("window.ifc2usdViewer.getSelectedGuid()") == wall_guid


def test_clicking_3d_object_syncs_tree_selection(page, served_url):
    _wait_for_load(page, served_url)

    wall_guid = _guid_by_name(page, "Wall North")
    _click_center_of(page, wall_guid)

    is_tree_selected = page.eval_on_selector(
        f'li[data-guid="{wall_guid}"]', "el => el.classList.contains('selected')"
    )
    assert is_tree_selected


def test_property_panel_shows_class_guid_and_custom_data(page, served_url):
    _wait_for_load(page, served_url)

    wall_guid = _guid_by_name(page, "Wall North")
    page.locator(f'[data-guid="{wall_guid}"] > .tree-label').click()

    panel_text = page.locator("#property-panel").inner_text()
    assert "Wall North" in panel_text
    assert "IfcWall" in panel_text
    assert wall_guid in panel_text


def test_property_panel_clears_when_nothing_selected(page, served_url):
    _wait_for_load(page, served_url)

    assert page.locator("#property-panel").inner_text().strip() == ""

    wall_guid = _guid_by_name(page, "Wall North")
    page.locator(f'[data-guid="{wall_guid}"] > .tree-label').click()
    assert page.locator("#property-panel").inner_text().strip() != ""


def test_property_panel_updates_when_selecting_another_object_in_3d(page, served_url):
    _wait_for_load(page, served_url)

    north_guid = _guid_by_name(page, "Wall North")
    east_guid = _guid_by_name(page, "Wall East")

    _click_center_of(page, north_guid)
    assert north_guid in page.locator("#property-panel").inner_text()

    _click_center_of(page, east_guid)
    panel_text = page.locator("#property-panel").inner_text()
    assert east_guid in panel_text
    assert north_guid not in panel_text
