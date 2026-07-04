"""SDF水平スライスのビューワー表示（Issue #29 / E5-3）のE2Eテスト。"""

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
def served_url_with_sdf(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("sdf_slice_e2e")
    usda = tmp_path / "minimal.usda"
    convert(FIXTURE, usda)

    workdir = tmp_path / "www"
    workdir.mkdir()
    build_serve_directory(usda, workdir, sdf_slices=True)

    server = make_server(workdir, port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    yield f"http://127.0.0.1:{port}/"

    server.shutdown()
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def served_url_without_sdf(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("sdf_slice_e2e_off")
    usda = tmp_path / "minimal.usda"
    convert(FIXTURE, usda)

    workdir = tmp_path / "www"
    workdir.mkdir()
    build_serve_directory(usda, workdir)  # sdf_slices既定offのまま

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
    # preserveDrawingBuffer(スクリーンショット画素検証に必要)は?e2eクエリで有効化する
    # (tests/test_section_clip_e2e.pyと同じ理由、viewer.jsのisE2ETest参照)。
    page.goto(f"{url}?e2e")
    page.wait_for_function("window.ifc2usdLoaded === true", timeout=10000)


def _first_guid(page):
    return page.evaluate("""
        () => {
            const tree = window.ifc2usdViewer.sceneDescription.tree;
            function firstLeafGuid(nodes) {
                for (const node of nodes) {
                    if (node.children.length === 0) return node.guid;
                    const found = firstLeafGuid(node.children);
                    if (found) return found;
                }
                return null;
            }
            return firstLeafGuid(tree);
        }
    """)


def _colored_pixel_counts(page):
    """canvasを2D canvasへdrawImageし、SDFスライスの配色(白=表面/青=内部/橙=外部)に
    近いピクセル数を数える（実際に画面へ描画されていることを検証するため。
    CLAUDE.mdの教訓通り、データが正しいことと画面に出ることは別に確認する）。"""
    return page.evaluate("""
        () => {
            const canvas = document.querySelector('#viewport canvas');
            const tmp = document.createElement('canvas');
            tmp.width = canvas.width;
            tmp.height = canvas.height;
            const ctx = tmp.getContext('2d');
            ctx.drawImage(canvas, 0, 0);
            const { data } = ctx.getImageData(0, 0, tmp.width, tmp.height);
            const close = (r, g, b, tr, tg, tb) =>
                Math.abs(r - tr) < 30 && Math.abs(g - tg) < 30 && Math.abs(b - tb) < 30;
            let white = 0, blue = 0, orange = 0;
            for (let i = 0; i < data.length; i += 4) {
                const r = data[i], g = data[i + 1], b = data[i + 2];
                if (close(r, g, b, 255, 255, 255)) white++;
                else if (close(r, g, b, 80, 140, 255)) blue++;
                else if (close(r, g, b, 255, 120, 80)) orange++;
            }
            return { white, blue, orange };
        }
    """)


def test_sdf_ui_disabled_without_selection(page, served_url_with_sdf):
    _wait_for_load(page, served_url_with_sdf)

    assert page.locator("#sdf-slice-toggle").is_disabled()
    assert page.locator("#sdf-slice-height-slider").is_disabled()


def test_sdf_asset_absent_keeps_ui_disabled_even_when_selected(page, served_url_without_sdf):
    """--sdf-slices無しでserveした場合、要素を選択してもSDFデータが無いため
    トグル/スライダーは無効のままであること。"""
    _wait_for_load(page, served_url_without_sdf)

    guid = _first_guid(page)
    assert guid is not None
    page.evaluate("(guid) => window.ifc2usdViewer.selectByGuid(guid)", guid)

    assert page.evaluate("(guid) => window.ifc2usdViewer.hasSdfSlicesFor(guid)", guid) is False
    assert page.locator("#sdf-slice-toggle").is_disabled()


def test_selecting_element_enables_sdf_slice_controls(page, served_url_with_sdf):
    _wait_for_load(page, served_url_with_sdf)

    guid = _first_guid(page)
    assert guid is not None
    assert page.evaluate("(guid) => window.ifc2usdViewer.hasSdfSlicesFor(guid)", guid) is True

    page.evaluate("(guid) => window.ifc2usdViewer.selectByGuid(guid)", guid)

    assert page.locator("#sdf-slice-toggle").is_enabled()
    assert page.locator("#sdf-slice-height-slider").is_enabled()


def test_enabling_sdf_slice_toggle_creates_overlay_mesh_and_renders_colors(page, served_url_with_sdf):
    _wait_for_load(page, served_url_with_sdf)

    guid = _first_guid(page)
    page.evaluate("(guid) => window.ifc2usdViewer.selectByGuid(guid)", guid)

    assert page.evaluate("window.ifc2usdViewer.getSdfSliceMesh()") is None

    page.locator("#sdf-slice-toggle").check()
    page.wait_for_timeout(100)  # 次のrAFで再描画されるのを待つ

    assert page.evaluate("window.ifc2usdViewer.getSdfSliceMesh() !== null") is True

    colors = _colored_pixel_counts(page)
    # narrow-band(band_width既定3)は表面付近全体を覆うため、白(表面)は必ず出る。
    # 壁は薄い(厚み<size)ため中心スライスに内部(青)が無いケースもあり得るので、
    # 白の出現のみを厳密条件とし、青/橙は出れば良い程度の緩い確認に留める。
    assert colors["white"] > 0


def test_unchecking_sdf_slice_toggle_removes_overlay_mesh(page, served_url_with_sdf):
    _wait_for_load(page, served_url_with_sdf)

    guid = _first_guid(page)
    page.evaluate("(guid) => window.ifc2usdViewer.selectByGuid(guid)", guid)
    page.locator("#sdf-slice-toggle").check()
    page.wait_for_timeout(100)
    assert page.evaluate("window.ifc2usdViewer.getSdfSliceMesh() !== null") is True

    page.locator("#sdf-slice-toggle").uncheck()
    page.wait_for_timeout(100)
    assert page.evaluate("window.ifc2usdViewer.getSdfSliceMesh()") is None


def test_moving_sdf_slice_height_slider_changes_overlay_z(page, served_url_with_sdf):
    _wait_for_load(page, served_url_with_sdf)

    guid = _first_guid(page)
    page.evaluate("(guid) => window.ifc2usdViewer.selectByGuid(guid)", guid)
    page.locator("#sdf-slice-toggle").check()
    page.wait_for_timeout(100)

    max_index = int(page.locator("#sdf-slice-height-slider").get_attribute("max"))
    if max_index < 1:
        pytest.skip("this element only has a single SDF slice; height slider has nothing to move")

    z_before = page.evaluate("window.ifc2usdViewer.getSdfSliceMesh().position.z")
    page.locator("#sdf-slice-height-slider").fill("0")
    page.wait_for_timeout(100)
    z_after = page.evaluate("window.ifc2usdViewer.getSdfSliceMesh().position.z")

    assert z_before != z_after
