"""表示モード（mesh/voxel/both）+ ボクセルLOD切替UIのE2Eテスト（Issue #15 / E3-7）。"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from ifc2usd import convert
from ifc2usd.serve import build_serve_directory, make_server
from tests.conftest import chromium_launch_kwargs

FIXTURE = Path(__file__).parent / "fixtures" / "minimal.ifc"


def _serve(tmp_path, name, voxel_sizes=(0.5,)):
    usda = tmp_path / "minimal.usda"
    convert(FIXTURE, usda)

    workdir = tmp_path / name
    workdir.mkdir()
    build_serve_directory(usda, workdir, voxel_sizes=voxel_sizes)

    server = make_server(workdir, port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{port}/"


@pytest.fixture(scope="module")
def served_url(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("display_mode_e2e")
    server, thread, url = _serve(tmp_path, "www", voxel_sizes=(0.5, 0.25))
    yield url
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


def _visibility(page):
    return page.evaluate("""
        () => ({
            mesh: window.ifc2usdViewer.getGlbRoot().visible,
            lods: window.ifc2usdViewer.voxelLods.map(l => l.mesh.visible),
        })
    """)


def test_default_mode_is_both_matching_prior_default_visibility(page, served_url):
    """Issue #14時点の既定表示（メッシュ+先頭LOD可視）から見た目が変わらないよう、
    既定選択は「both」にする。"""
    _wait_for_load(page, served_url)

    checked = page.evaluate(
        "document.querySelector('input[name=\"display-mode\"]:checked').value"
    )
    assert checked == "both"

    visibility = _visibility(page)
    assert visibility["mesh"] is True
    assert visibility["lods"] == [True, False]


def test_mesh_mode_hides_all_voxel_lods(page, served_url):
    _wait_for_load(page, served_url)

    page.check('input[name="display-mode"][value="mesh"]')
    visibility = _visibility(page)
    assert visibility["mesh"] is True
    assert visibility["lods"] == [False, False]


def test_voxel_mode_hides_mesh_and_shows_active_lod(page, served_url):
    _wait_for_load(page, served_url)

    page.check('input[name="display-mode"][value="voxel"]')
    visibility = _visibility(page)
    assert visibility["mesh"] is False
    assert visibility["lods"] == [True, False]


def test_both_mode_shows_mesh_and_active_lod(page, served_url):
    _wait_for_load(page, served_url)

    page.check('input[name="display-mode"][value="mesh"]')
    page.check('input[name="display-mode"][value="both"]')
    visibility = _visibility(page)
    assert visibility["mesh"] is True
    assert visibility["lods"] == [True, False]


def test_lod_select_has_one_option_per_lod_with_size_label(page, served_url):
    _wait_for_load(page, served_url)

    options = page.eval_on_selector_all(
        "#voxel-lod-select option", "els => els.map(e => e.textContent)"
    )
    assert options == ["0.5m", "0.25m"]


def test_switching_lod_changes_active_lod_only(page, served_url):
    _wait_for_load(page, served_url)

    page.check('input[name="display-mode"][value="voxel"]')
    page.select_option("#voxel-lod-select", "1")

    visibility = _visibility(page)
    assert visibility["mesh"] is False
    assert visibility["lods"] == [False, True]


def test_no_voxel_lods_lod_select_is_empty_and_mode_switch_does_not_crash(tmp_path, browser):
    """voxelizable要素の無いモデルではLOD選択肢が0件になり、voxel/bothモードへの
    切替もエラーにならないこと（Issue #14で確認済みのvoxels省略パスとの整合）。"""
    from pxr import Usd, UsdGeom

    no_elements_usda = tmp_path / "no_elements.usda"
    stage = Usd.Stage.CreateNew(str(no_elements_usda))
    root = UsdGeom.Xform.Define(stage, "/Model")
    stage.SetDefaultPrim(root.GetPrim())
    mesh = UsdGeom.Mesh.Define(stage, "/Model/mesh")
    mesh.CreatePointsAttr([(0, 0, 0), (1, 0, 0), (0, 1, 0)])
    mesh.CreateFaceVertexCountsAttr([3])
    mesh.CreateFaceVertexIndicesAttr([0, 1, 2])
    stage.GetRootLayer().Save()

    workdir = tmp_path / "www_no_voxels"
    workdir.mkdir()
    build_serve_directory(no_elements_usda, workdir)

    server = make_server(workdir, port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        page = browser.new_page(viewport={"width": 800, "height": 600})
        errors = []
        page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
        # console.errorだけでは、ハンドラ内で投げられた未捕捉例外(pageerror)を
        # 見逃してしまう。両方監視して初めて「本当にクラッシュしていない」と言える。
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        _wait_for_load(page, f"http://127.0.0.1:{port}/")

        option_count = page.eval_on_selector_all("#voxel-lod-select option", "els => els.length")
        assert option_count == 0

        page.check('input[name="display-mode"][value="voxel"]')
        page.check('input[name="display-mode"][value="both"]')
        assert errors == []
        page.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)
