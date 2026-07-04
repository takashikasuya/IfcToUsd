"""`ifc2usd serve`（静的配信）のテスト。

ディレクトリ組み立て（scene.json/GLB/静的ビューワーアセットの用意）と、
HTTPサーバーの起動・応答を分離してテストする。
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from ifc2usd import convert
from ifc2usd.serve import build_serve_directory, make_server

FIXTURE = Path(__file__).parent / "fixtures" / "minimal.ifc"


@pytest.fixture
def usda(tmp_path) -> Path:
    out = tmp_path / "minimal.usda"
    convert(FIXTURE, out)
    return out


def test_build_serve_directory_produces_scene_json_and_glb(usda, tmp_path):
    workdir = tmp_path / "www"
    workdir.mkdir()
    build_serve_directory(usda, workdir)

    scene_path = workdir / "scene.json"
    assert scene_path.is_file()
    scene = json.loads(scene_path.read_text(encoding="utf-8"))
    assert scene["version"] == 1
    assert scene["tree"][0]["path"] == "/IFC_Model/Site"

    glb_name = scene["assets"]["gltf"]
    assert (workdir / glb_name).is_file()


def test_build_serve_directory_produces_voxels_json(usda, tmp_path):
    """docs/viewer/spec.md §1.2/§4.1: serveはGLB/scene.jsonに加えvoxels.jsonも
    生成し、scene.jsonのassets.voxelsから参照できるようにする。"""
    workdir = tmp_path / "www"
    workdir.mkdir()
    build_serve_directory(usda, workdir)

    scene = json.loads((workdir / "scene.json").read_text(encoding="utf-8"))
    voxels_name = scene["assets"]["voxels"]
    voxels_path = workdir / voxels_name
    assert voxels_path.is_file()

    voxels = json.loads(voxels_path.read_text(encoding="utf-8"))
    assert voxels["version"] == 2
    assert len(voxels["lods"]) == 1
    assert voxels["lods"][0]["size"] == 0.5
    guids = {el["guid"] for el in voxels["lods"][0]["elements"]}
    assert len(guids) == 2  # 壁2枚


def test_build_serve_directory_voxel_sizes_are_configurable(usda, tmp_path):
    workdir = tmp_path / "www"
    workdir.mkdir()
    build_serve_directory(usda, workdir, voxel_sizes=(0.5, 0.25))

    scene = json.loads((workdir / "scene.json").read_text(encoding="utf-8"))
    voxels = json.loads((workdir / scene["assets"]["voxels"]).read_text(encoding="utf-8"))
    assert [lod["size"] for lod in voxels["lods"]] == [0.5, 0.25]


def test_build_serve_directory_omits_voxels_asset_when_no_elements(tmp_path):
    """ボクセル化可能な要素（GUID+class customData付きのmesh）が1つもないUSDでは、
    生のValueErrorで落ちるのではなくvoxels資産自体を省略する
    （GLBのみのビューワー表示は引き続き成立するため）。"""
    from pxr import Usd, UsdGeom

    no_elements_usda = tmp_path / "no_elements.usda"
    stage = Usd.Stage.CreateNew(str(no_elements_usda))
    root = UsdGeom.Xform.Define(stage, "/Model")
    stage.SetDefaultPrim(root.GetPrim())
    # customData(GUID/class)を持たないmesh: export_gltfはこれを描画できる
    # （elements_from_stageの対象外というだけで、GLB自体は空にならない）。
    mesh = UsdGeom.Mesh.Define(stage, "/Model/mesh")
    mesh.CreatePointsAttr([(0, 0, 0), (1, 0, 0), (0, 1, 0)])
    mesh.CreateFaceVertexCountsAttr([3])
    mesh.CreateFaceVertexIndicesAttr([0, 1, 2])
    stage.GetRootLayer().Save()

    workdir = tmp_path / "www"
    workdir.mkdir()
    build_serve_directory(no_elements_usda, workdir)

    scene = json.loads((workdir / "scene.json").read_text(encoding="utf-8"))
    assert "voxels" not in scene["assets"]


def test_build_serve_directory_omits_voxels_asset_when_elements_have_no_vertices(tmp_path):
    """elements_from_stageはGUID+class customDataとmesh子primがあれば要素を返すが、
    頂点0件の退化メッシュも要素として拾いうる。全要素が頂点0件だと
    build_voxel_json内部のscene_originがValueErrorで落ちるため、その手前で
    voxels.json自体を省略できることを確認する（「elementsがあるかどうか」ではなく
    「頂点を持つ要素があるかどうか」で判定する必要がある回帰テスト）。"""
    from pxr import Usd, UsdGeom

    no_vertices_usda = tmp_path / "no_vertices.usda"
    stage = Usd.Stage.CreateNew(str(no_vertices_usda))
    root = UsdGeom.Xform.Define(stage, "/Model")
    stage.SetDefaultPrim(root.GetPrim())

    element = UsdGeom.Xform.Define(stage, "/Model/Element")
    element.GetPrim().SetCustomDataByKey("GUID", "degenerate-guid")
    element.GetPrim().SetCustomDataByKey("class", "IfcWall")
    # customData付きのmesh子primはあるが、頂点は1つも無い(退化メッシュ)。
    UsdGeom.Mesh.Define(stage, "/Model/Element/mesh")
    stage.GetRootLayer().Save()

    workdir = tmp_path / "www"
    workdir.mkdir()
    build_serve_directory(no_vertices_usda, workdir)

    scene = json.loads((workdir / "scene.json").read_text(encoding="utf-8"))
    assert "voxels" not in scene["assets"]


def test_build_serve_directory_copies_viewer_assets(usda, tmp_path):
    workdir = tmp_path / "www"
    workdir.mkdir()
    build_serve_directory(usda, workdir)

    assert (workdir / "index.html").is_file()
    assert (workdir / "viewer.js").is_file()
    assert (workdir / "vendor" / "three.module.min.js").is_file()
    assert (workdir / "vendor" / "controls" / "OrbitControls.js").is_file()
    assert (workdir / "vendor" / "loaders" / "GLTFLoader.js").is_file()


def test_build_serve_directory_rejects_missing_usd(tmp_path):
    """存在しないパスは、pxr由来の分かりにくい例外ではなく明確なFileNotFoundErrorになる。"""
    workdir = tmp_path / "www"
    workdir.mkdir()
    with pytest.raises(FileNotFoundError):
        build_serve_directory(tmp_path / "does_not_exist.usda", workdir)


def test_server_serves_scene_json_and_index_html(usda, tmp_path):
    workdir = tmp_path / "www"
    workdir.mkdir()
    build_serve_directory(usda, workdir)

    server = make_server(workdir, port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/scene.json", timeout=5) as resp:
            assert resp.status == 200
            scene = json.loads(resp.read().decode("utf-8"))
            assert scene["version"] == 1

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/index.html", timeout=5) as resp:
            assert resp.status == 200
            assert b"<html" in resp.read().lower()

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/vendor/three.module.min.js", timeout=5) as resp:
            assert resp.status == 200
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_server_binds_to_localhost_only(usda, tmp_path):
    """外部からの意図しないアクセスを避けるため、127.0.0.1にのみバインドする。"""
    workdir = tmp_path / "www"
    workdir.mkdir()
    build_serve_directory(usda, workdir)

    server = make_server(workdir, port=0)
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()


def test_directory_listing_is_disabled_for_index_less_directories(usda, tmp_path):
    """vendor/のようにindex.htmlを持たないディレクトリでも、
    ファイル名一覧を返さない（404になる）。"""
    workdir = tmp_path / "www"
    workdir.mkdir()
    build_serve_directory(usda, workdir)

    server = make_server(workdir, port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/vendor/", timeout=5)
        assert excinfo.value.code == 404
    finally:
        server.shutdown()
        thread.join(timeout=5)
