"""断面クリップ平面（Z高さスライダー）のE2Eテスト（Issue #18 / E3-9）。"""

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
    tmp_path = tmp_path_factory.mktemp("section_clip_e2e")
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
    # preserveDrawingBuffer(canvasのdrawImage/getImageDataによる画素読み取りに必要)は
    # 実ユーザーに常時コストを払わせないよう既定offにしてあり、?e2eクエリで
    # 明示的に有効化する必要がある（viewer.jsのisE2ETest参照）。
    page.goto(f"{url}?e2e")
    page.wait_for_function("window.ifc2usdLoaded === true", timeout=10000)


def _non_background_pixel_count(page):
    """canvasを2D canvasへdrawImageし、背景色(0x202020)と明確に異なる
    ピクセル数を数える（断面クリップで実際にジオメトリが消えたことの検証用）。"""
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


def test_section_slider_defaults_to_no_clipping(page, served_url):
    _wait_for_load(page, served_url)

    box = page.evaluate("""
        () => {
            const box = window.ifc2usdViewer.getBoundingBox();
            return { min: box.min.y, max: box.max.y };
        }
    """)
    slider_value = page.evaluate(
        "() => Number(document.getElementById('section-height-slider').value)"
    )
    clip_height = page.evaluate("window.ifc2usdViewer.getSectionClipHeight()")

    assert slider_value == pytest.approx(box["max"])
    assert clip_height == pytest.approx(box["max"])


def test_setting_section_clip_height_updates_state(page, served_url):
    _wait_for_load(page, served_url)

    box = page.evaluate("""
        () => {
            const box = window.ifc2usdViewer.getBoundingBox();
            return { min: box.min.y, max: box.max.y };
        }
    """)
    mid = (box["min"] + box["max"]) / 2
    page.evaluate(f"window.ifc2usdViewer.setSectionClipHeight({mid})")

    assert page.evaluate("window.ifc2usdViewer.getSectionClipHeight()") == pytest.approx(mid)
    slider_value = page.evaluate(
        "() => Number(document.getElementById('section-height-slider').value)"
    )
    assert slider_value == pytest.approx(mid)


def test_lowering_section_clip_hides_upper_geometry(page, served_url):
    """スライダーを下げると、実際にレンダリングされた画素からジオメトリが
    消えること（renderer.clippingPlanesの状態だけでなく見た目を検証する）。"""
    _wait_for_load(page, served_url)

    box = page.evaluate("""
        () => {
            const box = window.ifc2usdViewer.getBoundingBox();
            return { min: box.min.y, max: box.max.y };
        }
    """)
    # モデル全体が画面に収まるようにフィットしてから比較する
    page.evaluate("window.ifc2usdViewer.fitAll()")
    page.wait_for_timeout(200)

    before = _non_background_pixel_count(page)
    assert before > 0  # 何かしら描画されている前提

    page.evaluate(f"window.ifc2usdViewer.setSectionClipHeight({box['min']})")
    page.wait_for_timeout(200)
    after = _non_background_pixel_count(page)

    assert after < before


def test_moving_slider_input_element_updates_clip_height(page, served_url):
    """UIのスライダー要素自体を操作したときも状態に反映されること。"""
    _wait_for_load(page, served_url)

    box = page.evaluate("""
        () => {
            const box = window.ifc2usdViewer.getBoundingBox();
            return { min: box.min.y, max: box.max.y };
        }
    """)
    mid = (box["min"] + box["max"]) / 2
    page.evaluate(f"""
        () => {{
            const slider = document.getElementById('section-height-slider');
            slider.value = "{mid}";
            slider.dispatchEvent(new Event('input', {{ bubbles: true }}));
        }}
    """)

    assert page.evaluate("window.ifc2usdViewer.getSectionClipHeight()") == pytest.approx(mid)
