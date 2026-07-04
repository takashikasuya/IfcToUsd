"""ボクセル描画（voxels.json v2 → InstancedMesh）のE2Eテスト（Issue #14 / E3-6）。"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from ifc2usd import convert
from ifc2usd.serve import build_serve_directory, make_server
from ifc2usd.voxel import morton_decode
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
    return server, thread, f"http://127.0.0.1:{port}/", workdir


@pytest.fixture(scope="module")
def served_url(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("voxel_viewer_e2e")
    server, thread, url, _workdir = _serve(tmp_path, "www")
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


def test_voxel_lod_is_loaded_with_correct_instance_count(page, served_url):
    _wait_for_load(page, served_url)

    lod_count = page.evaluate("window.ifc2usdViewer.voxelLods.length")
    assert lod_count == 1

    instance_count = page.evaluate("window.ifc2usdViewer.voxelLods[0].mesh.count")
    # 既知の解析値（tests/test_voxel_pointinstancer.pyのPointInstancer版と同じ):
    # Wall North + Wall East @0.5m = 60 + 48
    assert instance_count == 60 + 48


def test_voxel_instance_positions_match_origin_plus_index_times_size(page, served_url):
    """spec.md §2: indices(Morton)からのワールド座標復元が
    origin + index*size と一致すること。JS実装ではなく既にテスト済みの
    Python側morton_decodeで独立に期待値を計算し、突き合わせる。"""
    _wait_for_load(page, served_url)

    voxels = page.evaluate("""
        () => fetch(window.ifc2usdViewer.sceneDescription.assets.voxels).then(r => r.json())
    """)
    origin = voxels["origin"]
    lod = voxels["lods"][0]
    size = lod["size"]

    expected = set()
    for el in lod["elements"]:
        for code in el["indices"]:
            ix, iy, iz = morton_decode(code)
            expected.add(
                (
                    round(origin[0] + (ix + 0.5) * size, 6),
                    round(origin[1] + (iy + 0.5) * size, 6),
                    round(origin[2] + (iz + 0.5) * size, 6),
                )
            )

    actual = page.evaluate("""
        () => {
            const lod = window.ifc2usdViewer.voxelLods[0];
            const arr = lod.mesh.instanceMatrix.array;
            const positions = [];
            for (let i = 0; i < lod.mesh.count; i++) {
                const base = i * 16;
                positions.push([
                    Math.round(arr[base + 12] * 1e6) / 1e6,
                    Math.round(arr[base + 13] * 1e6) / 1e6,
                    Math.round(arr[base + 14] * 1e6) / 1e6,
                ]);
            }
            return positions;
        }
    """)

    assert {tuple(p) for p in actual} == expected


def test_morton_decode_matches_python_reference_across_fast_and_bigint_paths(page, served_url):
    """viewer.jsのmortonDecodeは閾値以下を通常のNumberビット演算、それを超える
    コードをBigIntで処理する2パス構成（速度対策）。両パスとも既にテスト済みの
    Python側morton_decodeと一致することを確認する。

    境界値のテストは重要: JSのシフト演算子はシフト量を32で余りを取るため、
    「コードが32bitに収まるかどうか」ではなく「ループが必要とする最大シフト量が
    31以下か」が閾値の条件になる（2^30-1なら安全、2^31-1だとシフト量33への
    ラップアラウンドで壊れる。これは実際にこのテストで一度検出したバグ）。"""
    _wait_for_load(page, served_url)

    codes = [
        0,
        1,
        0x3FFFFFFF,  # fast pathの境界値ちょうど（2^30-1）
        0x40000000,  # BigInt pathへ切り替わる直後
        0x7FFFFFFF,  # 旧・誤った閾値だった値（ここでBigInt pathに入ることを確認）
        (1 << 62) - 1,  # 21bit/軸に近い大きな値。Number.MAX_SAFE_INTEGERを超えるため
        # JS側にはBigIntリテラル("n"サフィックス)として渡さないと、引数自体が
        # doubleへの変換で精度落ちしてしまい正しくテストできない。
    ]
    for code in codes:
        expected = list(morton_decode(code))
        js_literal = f"{code}n" if code > (2**53 - 1) else str(code)
        actual = page.evaluate(f"window.ifc2usdViewer.mortonDecode({js_literal})")
        assert actual == expected, f"code={code}"


def test_voxel_instance_colors_match_element_color(page, served_url):
    _wait_for_load(page, served_url)

    voxels = page.evaluate("""
        () => fetch(window.ifc2usdViewer.sceneDescription.assets.voxels).then(r => r.json())
    """)
    lod = voxels["lods"][0]

    colors_by_guid = page.evaluate("""
        () => {
            const lod = window.ifc2usdViewer.voxelLods[0];
            const arr = lod.mesh.instanceColor.array;
            const result = {};
            for (let i = 0; i < lod.mesh.count; i++) {
                const guid = lod.instanceGuids[i];
                if (!(guid in result)) {
                    result[guid] = [arr[i * 3], arr[i * 3 + 1], arr[i * 3 + 2]];
                }
            }
            return result;
        }
    """)

    for el in lod["elements"]:
        actual_color = colors_by_guid[el["guid"]]
        for actual, expected in zip(actual_color, el["color"]):
            assert actual == pytest.approx(expected, abs=1e-4)


def test_first_voxel_lod_visible_by_default(page, served_url):
    """FR-5はボクセルが表示されること自体が受け入れ条件（表示モード切替はE3-7）。
    複数LODがある場合に同時に表示すると同じ体積を異なる粒度で二重描画してしまう
    ため、既定でアクティブなのは常に先頭（sizes指定順の1つ目）のLODのみとする。"""
    _wait_for_load(page, served_url)

    visible = page.evaluate("window.ifc2usdViewer.voxelLods[0].mesh.visible")
    assert visible is True


def test_no_voxel_lods_when_voxels_asset_absent(browser, tmp_path):
    """voxelizable要素が無いUSDではserveがvoxels資産自体を省略するため（Issue #14の
    server側実装）、ビューワーはvoxelLoadを試みずvoxelLodsが空になることを確認する。"""
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
        _wait_for_load(page, f"http://127.0.0.1:{port}/")

        assert page.evaluate("window.ifc2usdViewer.voxelLods.length") == 0
        assert errors == []
        page.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_multiple_lods_only_first_visible_by_default(browser, tmp_path):
    server, thread, url, _workdir = _serve(tmp_path, "www_multi_lod", voxel_sizes=(0.5, 0.25))
    try:
        page = browser.new_page(viewport={"width": 800, "height": 600})
        _wait_for_load(page, url)

        lod_count = page.evaluate("window.ifc2usdViewer.voxelLods.length")
        assert lod_count == 2

        visibilities = page.evaluate("window.ifc2usdViewer.voxelLods.map(l => l.mesh.visible)")
        assert visibilities == [True, False]
        page.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_broken_voxels_json_degrades_to_mesh_only(browser, tmp_path):
    """voxels.jsonの読み込み・パースに失敗しても、メッシュ表示自体は壊れず
    ロードが完了すること（ボクセルはあくまで付加的な情報という設計）。"""
    server, thread, url, workdir = _serve(tmp_path, "www_broken_voxels")
    try:
        voxels_files = list(workdir.glob("*_voxels.json"))
        assert len(voxels_files) == 1
        voxels_files[0].write_text("not valid json {{{", encoding="utf-8")

        page = browser.new_page(viewport={"width": 800, "height": 600})
        errors = []
        page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
        _wait_for_load(page, url)

        assert page.evaluate("window.ifc2usdViewer.voxelLods.length") == 0
        # メッシュは通常通りロードされている（エラーバナーは出ない）
        assert page.evaluate("window.ifc2usdViewer.modelRoot.children.length") > 0
        assert page.locator("#load-error-banner").count() == 0
        assert errors == []  # console.warnのみで、console.errorは出ない
        page.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_500k_instances_build_without_error(browser, tmp_path):
    """NFR-2（50万インスタンスまで操作可能）のうち、このサンドボックス環境で
    誠実に検証できるのは「1 InstancedMesh/LODという設計により、50万インスタンス
    でも構築・描画がエラーなく完了すること」まで。実際の対話的fpsは、
    tests/test_viewer_e2e.pyのNFR-1と同じ理由（このヘッドレス環境のレンダラーは
    SwiftShaderという純粋ソフトウェアラスタライザであり、実GPU環境を代表しない）
    により、ここでは測定・主張しない。"""
    import json as json_module

    server, thread, url, workdir = _serve(tmp_path, "www_large")
    try:
        voxels_files = list(workdir.glob("*_voxels.json"))
        assert len(voxels_files) == 1
        voxels_path = voxels_files[0]
        voxels = json_module.loads(voxels_path.read_text(encoding="utf-8"))

        total = 500_000
        indices = list(range(total))
        voxels["lods"] = [
            {
                "size": 0.5,
                "elements": [
                    {"guid": "synthetic", "class": "Synthetic", "name": "Synthetic", "color": [1, 0, 0], "indices": indices}
                ],
            }
        ]
        voxels_path.write_text(json_module.dumps(voxels), encoding="utf-8")

        page = browser.new_page(viewport={"width": 800, "height": 600})
        errors = []
        page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
        page.goto(url)
        page.wait_for_function("window.ifc2usdLoaded === true", timeout=30000)

        assert page.evaluate("window.ifc2usdViewer.voxelLods[0].mesh.count") == total
        assert errors == []
        page.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)
