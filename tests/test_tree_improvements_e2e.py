"""ツリー改良のE2Eテスト（Issue #44 / E8-3）。

折りたたみ、選択行の自動展開+スクロール、検索/絞り込み、色チップ、isolateを検証する。
"""

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
    tmp_path = tmp_path_factory.mktemp("tree_improvements_e2e")
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


def _li_locator(page, guid):
    return page.locator(f'li[data-guid="{guid}"]')


def _row_locator(page, guid):
    return page.locator(f'li[data-guid="{guid}"] > .tree-row')


def test_storey_expanded_by_default_and_toggle_collapses_it(page, served_url):
    _wait_for_load(page, served_url)

    # Storeyクラスの行を探す(名前で直接わからないのでclass表記から特定する)。
    storey_li = page.locator("li", has_text="IfcBuildingStorey").first
    toggle = storey_li.locator("> .tree-row > .tree-toggle").first
    assert toggle.inner_text() == "▾"

    child_ul = storey_li.locator("> ul").first
    assert child_ul.is_visible()

    toggle.click()
    assert toggle.inner_text() == "▸"
    assert not child_ul.is_visible()

    toggle.click()
    assert toggle.inner_text() == "▾"
    assert child_ul.is_visible()


def test_leaf_element_has_no_expand_toggle(page, served_url):
    _wait_for_load(page, served_url)
    guid = _guid_by_name(page, "Wall North")

    toggle = _row_locator(page, guid).locator(".tree-toggle")
    assert "tree-toggle-empty" in (toggle.get_attribute("class") or "")
    assert toggle.inner_text() == ""


def test_selecting_element_expands_collapsed_ancestor_and_scrolls(page, served_url):
    _wait_for_load(page, served_url)
    guid = _guid_by_name(page, "Wall North")

    storey_li = page.locator("li", has_text="IfcBuildingStorey").first
    toggle = storey_li.locator("> .tree-row > .tree-toggle").first
    toggle.click()  # 折りたたむ
    assert toggle.inner_text() == "▸"

    scroll_calls = page.evaluate(f"""
        () => {{
            const li = document.querySelector('li[data-guid="{guid}"]');
            let called = false;
            const original = li.scrollIntoView;
            li.scrollIntoView = (...args) => {{ called = true; return original.apply(li, args); }};
            window.__scrollCalled = () => called;
            return true;
        }}
    """)
    assert scroll_calls is True

    page.evaluate(f"window.ifc2usdViewer.selectByGuid({guid!r})")

    assert toggle.inner_text() == "▾"  # 選択で祖先が自動展開される
    assert page.evaluate("window.__scrollCalled()") is True


def test_search_filters_to_matching_rows_and_ancestors(page, served_url):
    _wait_for_load(page, served_url)
    wall_north = _guid_by_name(page, "Wall North")
    wall_east = _guid_by_name(page, "Wall East")

    page.locator("#tree-search-input").fill("Wall North")
    page.wait_for_timeout(250)  # デバウンス(150ms)を待つ

    assert _li_locator(page, wall_north).is_visible()
    assert not _li_locator(page, wall_east).is_visible()

    # マッチ部分がハイライトされる
    mark = _row_locator(page, wall_north).locator("mark.tree-match")
    assert mark.inner_text().lower() == "wall north"

    # クリアで全表示に戻る
    page.locator("#tree-search-input").fill("")
    page.wait_for_timeout(250)
    assert _li_locator(page, wall_east).is_visible()


def test_search_matches_by_guid(page, served_url):
    _wait_for_load(page, served_url)
    wall_north = _guid_by_name(page, "Wall North")
    wall_east = _guid_by_name(page, "Wall East")

    page.locator("#tree-search-input").fill(wall_north)
    page.wait_for_timeout(250)

    assert _li_locator(page, wall_north).is_visible()
    assert not _li_locator(page, wall_east).is_visible()


def test_color_chip_reflects_element_color_and_absent_for_spatial_nodes(page, served_url):
    _wait_for_load(page, served_url)
    wall_north = _guid_by_name(page, "Wall North")

    chip = _row_locator(page, wall_north).locator(".tree-color-chip")
    assert chip.count() == 1
    style = chip.get_attribute("style")
    assert "rgb(204, 51, 51)" in style  # (0.8, 0.2, 0.2) * 255

    storey_li = page.locator("li", has_text="IfcBuildingStorey").first
    storey_chip = storey_li.locator("> .tree-row > .tree-color-chip").first
    assert storey_chip.count() == 0


def _object_visible(page, guid):
    return page.evaluate(f"""
        () => {{
            let found = null;
            window.ifc2usdViewer.modelRoot.traverse((obj) => {{
                if (obj.userData && obj.userData.guid === {guid!r}) found = obj.visible;
            }});
            return found;
        }}
    """)


def _ancestors_all_visible(page, guid):
    """three.jsは親Object3D/Groupの.visible===falseで子孫の描画自体を打ち切るため、
    対象自身の.visibleがtrueでも祖先(gltf.pyが書き出すSite/Building/Storeyの
    Group)のいずれかがfalseなら結局画面には出ない。祖先チェーン全体を検証する
    （コードレビューで検出: 対象の祖先を非表示にしてしまい、対象自身も一緒に
    消えていたバグの回帰テスト）。"""
    return page.evaluate(f"""
        () => {{
            let obj = null;
            window.ifc2usdViewer.modelRoot.traverse((o) => {{
                if (o.userData && o.userData.guid === {guid!r}) obj = o;
            }});
            if (!obj) return null;
            let current = obj;
            while (current && current !== window.ifc2usdViewer.modelRoot) {{
                if (!current.visible) return false;
                current = current.parent;
            }}
            return true;
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


def test_isolate_hides_everything_outside_subtree_and_toggle_restores(page, served_url):
    _wait_for_load(page, served_url)
    wall_north = _guid_by_name(page, "Wall North")
    wall_east = _guid_by_name(page, "Wall East")

    page.evaluate("window.ifc2usdViewer.fitAll()")
    page.wait_for_timeout(150)
    before_isolate_pixels = _non_background_pixel_count(page)
    assert before_isolate_pixels > 0

    row = _row_locator(page, wall_north)
    row.hover()
    row.locator(".tree-isolate-btn").click()

    assert page.evaluate("window.ifc2usdViewer.getIsolatedGuid()") == wall_north

    assert _object_visible(page, wall_north) is True
    assert _ancestors_all_visible(page, wall_north) is True  # 祖先(Site/Building/Storey)も可視のまま
    assert _object_visible(page, wall_east) is False
    assert _li_locator(page, wall_east).locator(".tree-visibility").is_checked() is False

    # データ上の.visibleフラグだけでなく、実際に画面へ何か描画されることまで確認する
    # (CLAUDE.mdの教訓: データが正しいことと画面に出ることは別)。isolate対象が
    # 祖先ごと隠れてしまっていた場合、ここは0になる。
    page.wait_for_timeout(150)
    isolated_pixels = _non_background_pixel_count(page)
    assert isolated_pixels > 0

    # 同じisolateボタンをもう一度押すと解除される
    row.locator(".tree-isolate-btn").click()
    assert page.evaluate("window.ifc2usdViewer.getIsolatedGuid()") is None
    assert _object_visible(page, wall_east) is True
    assert _li_locator(page, wall_east).locator(".tree-visibility").is_checked() is True
