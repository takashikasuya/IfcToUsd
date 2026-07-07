"""ボクセル描画がほぼ真っ黒になるバグの修正のE2Eテスト（Issue #39 / E8-6）。

instanceColorバッファの値（test_voxel_viewer_e2e.pyのtest_voxel_instance_colors_match_element_color）
が正しくても、実際に画面へ正しい色で描画されるとは限らない（CLAUDE.md記載のgltf.py
metallic/roughnessバグと同じ教訓）。このテストは実際にレンダリングされた画素を
サンプリングし、ボクセル表示モードで各壁の色が正しく画面に反映されることを検証する。
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
    tmp_path = tmp_path_factory.mktemp("voxel_rendering_color_e2e")
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
    # preserveDrawingBuffer(画素検証に必要)は?e2eクエリで有効化する
    # (tests/test_section_clip_e2e.pyと同じ理由、viewer.jsのisE2ETest参照)。
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


# test_viewer_e2e.py の_sample_pixel_at_guidと同じ理由（薄い壁は視点方向によって
# 自己遮蔽/逆光面しか見えず、色が正しくてもほぼ黒くレンダリングされることがある）
# で複数方向を試し、最も彩度の高い画素を採用する。
_CANDIDATE_VIEW_DIRECTIONS = [
    (5, 10, 7),  # DirectionalLightと同じ向き
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


def _sample_voxel_pixel_at_guid(page, guid):
    page.evaluate('window.ifc2usdViewer.setDisplayMode("voxel")')
    best = (32, 32, 32)  # 背景色（何も当たらなかった場合のフォールバック）
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


def test_voxel_display_is_not_near_black_for_red_wall(page, served_url):
    """Wall North(displayColor (0.8, 0.2, 0.2))はボクセル表示モードで
    赤みがかった明るい色でレンダリングされるべきで、Issue #39のバグ下では
    ほぼ(0, 0, 0)の真っ黒になっていた。"""
    _wait_for_load(page, served_url)
    guid = _guid_by_name(page, "Wall North")

    r, g, b = _sample_voxel_pixel_at_guid(page, guid)

    assert max(r, g, b) > 60, f"voxel pixel too dark: rgb=({r},{g},{b})"
    assert r > g and r > b, f"expected red-dominant color, got rgb=({r},{g},{b})"


def test_voxel_display_is_not_near_black_for_blue_wall(page, served_url):
    """Wall East(displayColor (0.2, 0.5, 0.8))についても同様に検証する。"""
    _wait_for_load(page, served_url)
    guid = _guid_by_name(page, "Wall East")

    r, g, b = _sample_voxel_pixel_at_guid(page, guid)

    assert max(r, g, b) > 60, f"voxel pixel too dark: rgb=({r},{g},{b})"
    assert b > r and b > g, f"expected blue-dominant color, got rgb=({r},{g},{b})"
