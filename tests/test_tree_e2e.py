"""階層ツリーUI（scene.json→DOM）+ 表示切替 + ツリー→3Dハイライト同期のE2Eテスト。"""

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
    tmp_path = tmp_path_factory.mktemp("tree_e2e")
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


def test_tree_reflects_scene_json_hierarchy(page, served_url):
    _wait_for_load(page, served_url)

    labels = page.eval_on_selector_all(
        "#tree-panel [data-guid] > .tree-label", "els => els.map(e => e.textContent)"
    )
    joined = " ".join(labels)
    assert "Fixture Site" in joined
    assert "Fixture Building" in joined
    assert "Ground Floor" in joined
    assert "Wall North" in joined
    assert "Wall East" in joined

    # 階層構造: Storey配下にWallのノードがネストしている
    nested = page.eval_on_selector(
        '[data-guid] ul li [data-guid] > .tree-label:has-text("Wall North")',
        "el => el !== null",
    )
    assert nested


def test_visibility_toggle_hides_object_in_3d_scene(page, served_url):
    _wait_for_load(page, served_url)

    wall_guid = page.evaluate("""
        () => {
            let found = null;
            window.ifc2usdViewer.modelRoot.traverse((obj) => {
                if (obj.userData && obj.userData.name === 'Wall North') found = obj.userData.guid;
            });
            return found;
        }
    """)
    assert wall_guid

    def object_visible():
        return page.evaluate(f"""
            () => {{
                let visible = null;
                window.ifc2usdViewer.modelRoot.traverse((obj) => {{
                    if (obj.userData && obj.userData.guid === "{wall_guid}") visible = obj.visible;
                }});
                return visible;
            }}
        """)

    assert object_visible() is True

    checkbox = page.locator(f'[data-guid="{wall_guid}"] > .tree-visibility')
    checkbox.uncheck()
    assert object_visible() is False

    checkbox.check()
    assert object_visible() is True


def test_clicking_tree_node_highlights_corresponding_3d_object(page, served_url):
    _wait_for_load(page, served_url)

    wall_guid = page.evaluate("""
        () => {
            let found = null;
            window.ifc2usdViewer.modelRoot.traverse((obj) => {
                if (obj.userData && obj.userData.name === 'Wall North') found = obj.userData.guid;
            });
            return found;
        }
    """)

    def is_highlighted():
        return page.evaluate(f"""
            () => window.ifc2usdViewer.getSelectedGuid && window.ifc2usdViewer.getSelectedGuid() === "{wall_guid}"
        """)

    assert not is_highlighted()

    page.locator(f'[data-guid="{wall_guid}"] > .tree-label').click()
    assert is_highlighted()


def test_selecting_another_node_deselects_the_previous_one(page, served_url):
    _wait_for_load(page, served_url)

    north_guid, east_guid = page.evaluate("""
        () => {
            const guids = {};
            window.ifc2usdViewer.modelRoot.traverse((obj) => {
                if (obj.userData && obj.userData.name === 'Wall North') guids.north = obj.userData.guid;
                if (obj.userData && obj.userData.name === 'Wall East') guids.east = obj.userData.guid;
            });
            return [guids.north, guids.east];
        }
    """)

    page.locator(f'[data-guid="{north_guid}"] > .tree-label').click()
    assert page.evaluate("window.ifc2usdViewer.getSelectedGuid()") == north_guid

    page.locator(f'[data-guid="{east_guid}"] > .tree-label').click()
    assert page.evaluate("window.ifc2usdViewer.getSelectedGuid()") == east_guid


def test_tree_dom_matches_all_scene_json_nodes(page, served_url):
    """scene.jsonのノード数とツリーDOM上のノード数が一致する（欠落や重複がない）。"""
    _wait_for_load(page, served_url)

    scene_node_count = page.evaluate("""
        () => {
            let count = 0;
            function walk(nodes) {
                for (const n of nodes) {
                    count++;
                    walk(n.children);
                }
            }
            walk(window.ifc2usdViewer.sceneDescription.tree);
            return count;
        }
    """)
    dom_node_count = page.eval_on_selector_all("#tree-panel [data-guid]", "els => els.length")
    assert dom_node_count == scene_node_count
