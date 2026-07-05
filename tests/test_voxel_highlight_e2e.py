"""ボクセルInstancedMesh選択時の個別ハイライトのE2Eテスト（Issue #37 / E7-3）。"""

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
    tmp_path = tmp_path_factory.mktemp("voxel_highlight_e2e")
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


def _instance_colors_for_guid(page, guid, lod_index=0):
    return page.evaluate(f"""
        (guid) => {{
            const lod = window.ifc2usdViewer.voxelLods[{lod_index}];
            const arr = lod.mesh.instanceColor.array;
            const colors = [];
            for (let i = 0; i < lod.instanceGuids.length; i++) {{
                if (lod.instanceGuids[i] !== guid) continue;
                colors.push([arr[i * 3], arr[i * 3 + 1], arr[i * 3 + 2]]);
            }}
            return colors;
        }}
    """, guid)


def test_selecting_element_changes_its_voxel_instance_colors(page, served_url):
    _wait_for_load(page, served_url)

    voxels = page.evaluate("""
        () => fetch(window.ifc2usdViewer.sceneDescription.assets.voxels).then(r => r.json())
    """)
    element_colors = {el["guid"]: el["color"] for el in voxels["lods"][0]["elements"]}

    wall_guid = _guid_by_name(page, "Wall North")
    original_color = element_colors[wall_guid]

    before = _instance_colors_for_guid(page, wall_guid)
    assert len(before) > 0
    for c in before:
        assert c == pytest.approx(original_color, abs=1e-4)

    page.evaluate("(guid) => window.ifc2usdViewer.selectByGuid(guid)", wall_guid)

    after = _instance_colors_for_guid(page, wall_guid)
    assert len(after) == len(before)
    for c in after:
        assert c != pytest.approx(original_color, abs=1e-4)


def test_selecting_another_element_restores_previous_and_highlights_new(page, served_url):
    _wait_for_load(page, served_url)

    voxels = page.evaluate("""
        () => fetch(window.ifc2usdViewer.sceneDescription.assets.voxels).then(r => r.json())
    """)
    element_colors = {el["guid"]: el["color"] for el in voxels["lods"][0]["elements"]}

    wall_north = _guid_by_name(page, "Wall North")
    wall_east = _guid_by_name(page, "Wall East")

    page.evaluate("(guid) => window.ifc2usdViewer.selectByGuid(guid)", wall_north)
    page.evaluate("(guid) => window.ifc2usdViewer.selectByGuid(guid)", wall_east)

    north_colors = _instance_colors_for_guid(page, wall_north)
    for c in north_colors:
        assert c == pytest.approx(element_colors[wall_north], abs=1e-4)

    east_colors = _instance_colors_for_guid(page, wall_east)
    for c in east_colors:
        assert c != pytest.approx(element_colors[wall_east], abs=1e-4)


def test_highlighting_one_element_does_not_affect_other_elements(page, served_url):
    _wait_for_load(page, served_url)

    voxels = page.evaluate("""
        () => fetch(window.ifc2usdViewer.sceneDescription.assets.voxels).then(r => r.json())
    """)
    element_colors = {el["guid"]: el["color"] for el in voxels["lods"][0]["elements"]}

    wall_north = _guid_by_name(page, "Wall North")
    wall_east = _guid_by_name(page, "Wall East")

    page.evaluate("(guid) => window.ifc2usdViewer.selectByGuid(guid)", wall_north)

    east_colors = _instance_colors_for_guid(page, wall_east)
    assert len(east_colors) > 0
    for c in east_colors:
        assert c == pytest.approx(element_colors[wall_east], abs=1e-4)


def test_highlight_color_matches_expected_lerp_toward_highlight_hue(page, served_url):
    """コードレビュー指摘: 「変化したこと」だけでなく「正しい値に変化したこと」を
    検証する（色/ブレンド率の取り違えを拾えるように）。期待値はviewer.js自身の
    実装詳細（ハイライト色0x3355ff、lerp率0.6）を、three.jsを別途動的import
    して独立に計算する——アプリのランタイム状態を再利用せず、素のColor数学
    として同じ計算を再現するだけなので、テストが実装を追認するだけにはならない。"""
    _wait_for_load(page, served_url)

    voxels = page.evaluate("""
        () => fetch(window.ifc2usdViewer.sceneDescription.assets.voxels).then(r => r.json())
    """)
    element_colors = {el["guid"]: el["color"] for el in voxels["lods"][0]["elements"]}

    wall_guid = _guid_by_name(page, "Wall North")
    original_color = element_colors[wall_guid]

    page.evaluate("(guid) => window.ifc2usdViewer.selectByGuid(guid)", wall_guid)
    actual = _instance_colors_for_guid(page, wall_guid)[0]

    expected = page.evaluate("""
        async (original) => {
            const THREE = await import('/vendor/three.module.min.js');
            const base = new THREE.Color(original[0], original[1], original[2]);
            const highlight = new THREE.Color(0x3355ff);
            return base.lerp(highlight, 0.6).toArray();
        }
    """, original_color)

    assert actual == pytest.approx(expected, abs=1e-4)


def test_highlight_applies_across_all_voxel_lods(page, browser, tmp_path):
    """コードレビュー指摘: 出荷済みテストは全てLOD1つ(既定のvoxel_sizes)しか
    検証しておらず、「全LODに対してハイライトが適用される」という実装の中で
    最も見落としやすい挙動がテスト対象外だった。複数LODでserveし、
    アクティブでない側のLODにもハイライトが及ぶことを確認する。"""
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

        lod_count = page.evaluate("window.ifc2usdViewer.voxelLods.length")
        assert lod_count == 2

        voxels = page.evaluate("""
            () => fetch(window.ifc2usdViewer.sceneDescription.assets.voxels).then(r => r.json())
        """)
        element_colors = {el["guid"]: el["color"] for el in voxels["lods"][0]["elements"]}

        wall_guid = _guid_by_name(page, "Wall North")
        original_color = element_colors[wall_guid]

        for lod_index in (0, 1):
            before = _instance_colors_for_guid(page, wall_guid, lod_index=lod_index)
            assert len(before) > 0
            for c in before:
                assert c == pytest.approx(original_color, abs=1e-4)

        page.evaluate("(guid) => window.ifc2usdViewer.selectByGuid(guid)", wall_guid)

        for lod_index in (0, 1):
            after = _instance_colors_for_guid(page, wall_guid, lod_index=lod_index)
            assert len(after) > 0
            for c in after:
                assert c != pytest.approx(original_color, abs=1e-4)

        page.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)


# CLAUDE.mdの教訓（データが正しいことと画面に出ることは別）に従い、当初は
# 画素レベルのスクリーンショット比較（highlight前後でcanvas全体のバイト列が
# 変わること）も書いていたが、その過程で本Issueとは別の既存バグを発見した:
# ボクセル専用表示モード（InstancedMesh、MeshStandardMaterial+vertexColors）は
# このヘッドレス環境(SwiftShader)のライティング下で、要素の色に関わらずほぼ
# 真っ黒（既存のmeshモードの平均輝度が約73/255なのに対し、voxelモードは約12/255）
# にレンダリングされる、既存の未報告バグ（Issue #39として登録、E7-3のスコープ外）。
# この状態では画素比較テストが「本当にハイライトが機能しているか」ではなく
# 「たまたま暗すぎて差が検出できるか」を検証してしまい信頼できないため、
# 上のinstanceColorバッファ直接検証（この repo で既存の
# test_voxel_instance_colors_match_element_color と同じ手法）で十分な回帰保護
# とし、画素比較テストはIssue #39解決後に追加する。
