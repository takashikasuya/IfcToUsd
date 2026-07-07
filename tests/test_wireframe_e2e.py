"""ワイヤフレーム表示トグルのE2Eテスト。"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from ifc2usd import convert
from ifc2usd.serve import build_serve_directory, make_server
from tests.conftest import chromium_launch_kwargs

FIXTURE = Path(__file__).parent / "fixtures" / "minimal.ifc"


@pytest.fixture(scope="module")
def served_url(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("wireframe_e2e")
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


def _mesh_material_wireframe_flags(page):
    return page.evaluate("""
        () => {
            const flags = [];
            window.ifc2usdViewer.getGlbRoot().traverse((child) => {
                if (child.isMesh && child.material) flags.push(child.material.wireframe);
            });
            return flags;
        }
    """)


def _voxel_material_wireframe_flags(page):
    return page.evaluate("""
        () => window.ifc2usdViewer.voxelLods.map((lod) => lod.mesh.material.wireframe)
    """)


def _non_background_pixel_count(page):
    """tests/test_section_clip_e2e.pyと同じ手法: 背景色(0x202020)と明確に異なる
    ピクセル数を数える。ワイヤフレームは塗り面より被覆率が大きく下がるため、
    実際に画面上の見た目が変わったことの検証に使える。"""
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


def test_wireframe_toggle_unchecked_by_default(page, served_url):
    _wait_for_load(page, served_url)
    assert page.locator("#wireframe-toggle").is_checked() is False

    for flag in _mesh_material_wireframe_flags(page):
        assert flag is False
    for flag in _voxel_material_wireframe_flags(page):
        assert flag is False


def test_checking_wireframe_toggle_sets_wireframe_on_mesh_and_voxel_materials(page, served_url):
    _wait_for_load(page, served_url)

    page.locator("#wireframe-toggle").check()

    mesh_flags = _mesh_material_wireframe_flags(page)
    assert len(mesh_flags) > 0
    for flag in mesh_flags:
        assert flag is True

    voxel_flags = _voxel_material_wireframe_flags(page)
    assert len(voxel_flags) > 0
    for flag in voxel_flags:
        assert flag is True


def test_unchecking_wireframe_toggle_reverts_to_filled(page, served_url):
    _wait_for_load(page, served_url)

    page.locator("#wireframe-toggle").check()
    page.locator("#wireframe-toggle").uncheck()

    for flag in _mesh_material_wireframe_flags(page):
        assert flag is False
    for flag in _voxel_material_wireframe_flags(page):
        assert flag is False


def test_wireframe_is_visible_on_screen_for_mesh_display_mode(page, served_url):
    """CLAUDE.mdの教訓通り、データ(material.wireframeの値)が変わることと実際に
    画面へ描画されることは別に確認する。メッシュ表示モード（Issue #39のボクセル
    暗すぎ問題の影響を受けない）で、ワイヤフレーム化により塗りつぶし面が線だけに
    なり被覆ピクセル数が明確に減ることを確認する。"""
    _wait_for_load(page, served_url)
    page.check('input[name="display-mode"][value="mesh"]')
    page.evaluate("() => window.ifc2usdViewer.fitAll()")
    page.wait_for_timeout(150)

    filled_count = _non_background_pixel_count(page)

    page.locator("#wireframe-toggle").check()
    page.wait_for_timeout(150)
    wireframe_count = _non_background_pixel_count(page)

    assert filled_count > 0
    assert wireframe_count < filled_count


def test_wireframe_applies_across_all_voxel_lods(browser, tmp_path):
    """コードレビュー指摘: 出荷済みテストは既定の単一LODしか検証しておらず、
    「全LODに対してwireframeが適用される」という、buildVoxelLodsのコンストラクタ
    オプションとsetWireframeEnabledのループの両方が担う挙動が未検証だった。"""
    usda = tmp_path / "minimal.usda"
    convert(FIXTURE, usda)
    workdir = tmp_path / "www_multi_lod"
    workdir.mkdir()
    build_serve_directory(usda, workdir, voxel_sizes=(0.5, 1.0))

    server = make_server(workdir, port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        page = browser.new_page(viewport={"width": 1000, "height": 700})
        _wait_for_load(page, f"http://127.0.0.1:{port}/")

        assert page.evaluate("window.ifc2usdViewer.voxelLods.length") == 2

        page.locator("#wireframe-toggle").check()

        voxel_flags = _voxel_material_wireframe_flags(page)
        assert len(voxel_flags) == 2
        for flag in voxel_flags:
            assert flag is True

        page.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_toggling_wireframe_before_assets_finish_loading_is_applied_once_ready(page, served_url):
    """コードレビュー指摘: buildVoxelLodsの`wireframe: wireframeEnabled`
    コンストラクタ引数と、loadScene内の`if (wireframeEnabled) setWireframeEnabled(true)`
    は、どちらもglTF/voxels.jsonの非同期取得が終わる前にトグルされたケースのために
    存在するコードだが、既存テストは全て読み込み完了を待ってからトグルしており
    このタイミングを一切踏んでいなかった。glTF/voxels.jsonの応答を意図的に遅らせ、
    「取得中にトグル→完了後に正しく反映される」ことを検証する。"""

    def _delay_route(route):
        time.sleep(0.3)
        route.continue_()

    page.route("**/*.glb", _delay_route)
    page.route("**/*_voxels.json", _delay_route)

    page.goto(f"{served_url}?e2e")
    # viewer.jsのモジュールトップレベル同期処理（wireframeToggleの初期状態読み取り・
    # イベントリスナー登録、loadScene()の起動）は完了しているが、上でルートに
    # 仕込んだ遅延によりglTF/voxels.jsonの非同期取得はまだ終わっていないはずの
    # タイミングでトグルする。
    page.wait_for_timeout(50)
    page.locator("#wireframe-toggle").check()

    page.wait_for_function("window.ifc2usdLoaded === true", timeout=10000)

    mesh_flags = _mesh_material_wireframe_flags(page)
    assert len(mesh_flags) > 0
    for flag in mesh_flags:
        assert flag is True

    voxel_flags = _voxel_material_wireframe_flags(page)
    assert len(voxel_flags) > 0
    for flag in voxel_flags:
        assert flag is True
