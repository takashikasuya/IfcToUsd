"""Web ビューワー（viewer.js）のE2Eテスト。

`ifc2usd serve` 相当の静的配信を実際に起動し、Playwright（Chromium）で
GLB表示・カメラ操作（orbit/pan/zoom/全体フィット）・Z-UP吸収を検証する。
"""

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
    tmp_path = tmp_path_factory.mktemp("viewer_e2e")
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
    p = browser.new_page(viewport={"width": 800, "height": 600})
    yield p
    p.close()


def _wait_for_load(page, url):
    console_errors = []
    page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
    page.goto(url)
    page.wait_for_function("window.ifc2usdLoaded === true", timeout=10000)
    return console_errors


def test_page_loads_without_console_errors(page, served_url):
    errors = _wait_for_load(page, served_url)
    assert errors == []


def test_canvas_and_webgl_context_created(page, served_url):
    _wait_for_load(page, served_url)
    has_canvas = page.evaluate("document.querySelectorAll('#viewport canvas').length > 0")
    assert has_canvas

    context_type = page.evaluate("window.ifc2usdViewer.renderer.getContext().constructor.name")
    assert "WebGL" in context_type


def test_model_bounding_box_matches_expected_world_extent(page, served_url):
    """Z-UP吸収後も、モデルのワールド寸法（5.2 x 4.0 x 3.0m）自体は変わらない。"""
    _wait_for_load(page, served_url)

    extents = page.evaluate("""
        () => {
            const box = window.ifc2usdViewer.getBoundingBox();
            return [box.max.x - box.min.x, box.max.y - box.min.y, box.max.z - box.min.z];
        }
    """)
    assert sorted(round(e, 2) for e in extents) == [3.0, 4.0, 5.2]


def test_fit_all_centers_and_frames_the_model(page, served_url):
    _wait_for_load(page, served_url)
    page.evaluate("window.ifc2usdViewer.fitAll()")

    target = page.evaluate("window.ifc2usdViewer.controls.target.toArray()")
    box_center = page.evaluate("""
        () => {
            const box = window.ifc2usdViewer.getBoundingBox();
            const c = box.getCenter(new box.min.constructor());
            return [c.x, c.y, c.z];
        }
    """)
    for a, b in zip(target, box_center):
        assert abs(a - b) < 1e-3


def test_orbit_drag_changes_camera_position(page, served_url):
    _wait_for_load(page, served_url)
    before = page.evaluate("window.ifc2usdViewer.camera.position.toArray()")

    canvas_box = page.eval_on_selector("#viewport canvas", "el => { const r = el.getBoundingClientRect(); return {x: r.x, y: r.y, w: r.width, h: r.height}; }")
    cx = canvas_box["x"] + canvas_box["w"] / 2
    cy = canvas_box["y"] + canvas_box["h"] / 2

    page.mouse.move(cx, cy)
    page.mouse.down()
    page.mouse.move(cx + 150, cy + 60, steps=10)
    page.mouse.up()
    page.wait_for_timeout(200)  # OrbitControlsのdampingが収束するのを待つ

    after = page.evaluate("window.ifc2usdViewer.camera.position.toArray()")
    moved = any(abs(a - b) > 1e-3 for a, b in zip(before, after))
    assert moved


def test_zoom_wheel_changes_camera_distance(page, served_url):
    _wait_for_load(page, served_url)

    def distance_to_target():
        return page.evaluate("""
            () => {
                const c = window.ifc2usdViewer.camera.position;
                const t = window.ifc2usdViewer.controls.target;
                return Math.hypot(c.x - t.x, c.y - t.y, c.z - t.z);
            }
        """)

    before = distance_to_target()

    canvas_box = page.eval_on_selector("#viewport canvas", "el => { const r = el.getBoundingClientRect(); return {x: r.x, y: r.y, w: r.width, h: r.height}; }")
    page.mouse.move(canvas_box["x"] + canvas_box["w"] / 2, canvas_box["y"] + canvas_box["h"] / 2)
    page.mouse.wheel(0, -400)  # ズームイン
    page.wait_for_timeout(200)

    after = distance_to_target()
    assert after < before


def test_z_up_data_is_reoriented_for_three_js_y_up(page, served_url):
    """USDのZ-UPを吸収するルート回転が適用されている（X軸-90度）。"""
    _wait_for_load(page, served_url)
    rotation_x = page.evaluate("window.ifc2usdViewer.modelRoot.rotation.x")
    assert abs(rotation_x - (-1.5707963267948966)) < 1e-6
