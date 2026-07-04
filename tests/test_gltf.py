"""gltf.py（USD→GLBエクスポート）のテスト。

`tests/fixtures/minimal.ifc` の壁2枚を変換したUSDをGLBへエクスポートし、
色・階層・extras.guid をtrimeshで読み戻して検証する。
"""

from __future__ import annotations

import struct
import json as jsonlib
from pathlib import Path

import pytest
import trimesh
from pxr import Usd, UsdGeom

from ifc2usd import convert
from ifc2usd.gltf import export_gltf

FIXTURE = Path(__file__).parent / "fixtures" / "minimal.ifc"

EXPECTED_COLORS = {
    "Wall North": (0.8, 0.2, 0.2),
    "Wall East": (0.2, 0.5, 0.8),
}


def _gltf_json_from_glb(path: Path) -> dict:
    data = path.read_bytes()
    json_len = struct.unpack("<I", data[12:16])[0]
    return jsonlib.loads(data[20 : 20 + json_len])


def test_export_produces_valid_glb(tmp_path):
    usda = tmp_path / "minimal.usda"
    convert(FIXTURE, usda)
    stage = Usd.Stage.Open(str(usda))

    out = tmp_path / "minimal.glb"
    result_path = export_gltf(stage, str(out))
    assert result_path == str(out)
    assert out.is_file()

    reopened = trimesh.load(str(out))
    assert isinstance(reopened, trimesh.Scene)
    assert len(reopened.geometry) == 2  # 壁2枚


def test_node_hierarchy_mirrors_usd_tree(tmp_path):
    usda = tmp_path / "minimal.usda"
    convert(FIXTURE, usda)
    stage = Usd.Stage.Open(str(usda))

    out = tmp_path / "minimal.glb"
    export_gltf(stage, str(out))

    reopened = trimesh.load(str(out))
    nodes = set(reopened.graph.nodes)
    # USD階層: IFC_Model -> Site -> Building -> Storey_* -> Element_*
    assert "Site" in nodes
    assert "Building" in nodes
    assert any(n.startswith("Storey_") for n in nodes)
    assert any(n.startswith("Element_") for n in nodes)

    # Site -> Building の親子関係が保たれている
    edge_data = reopened.graph.transforms.edge_data
    assert ("Site", "Building") in edge_data


def test_node_extras_contain_guid_and_class(tmp_path):
    usda = tmp_path / "minimal.usda"
    convert(FIXTURE, usda)
    stage = Usd.Stage.Open(str(usda))

    out = tmp_path / "minimal.glb"
    export_gltf(stage, str(out))

    gltf = _gltf_json_from_glb(out)
    wall_nodes = [n for n in gltf["nodes"] if n.get("extras", {}).get("class") == "IfcWall"]
    assert len(wall_nodes) == 2
    for node in wall_nodes:
        assert "guid" in node["extras"]
        assert node["extras"]["guid"]  # 空でない
        assert "name" in node["extras"]

    names = {n["extras"]["name"] for n in wall_nodes}
    assert names == set(EXPECTED_COLORS)


def test_wall_colors_reflected_as_pbr_base_color(tmp_path):
    usda = tmp_path / "minimal.usda"
    convert(FIXTURE, usda)
    stage = Usd.Stage.Open(str(usda))

    out = tmp_path / "minimal.glb"
    export_gltf(stage, str(out))

    gltf = _gltf_json_from_glb(out)
    node_by_geom_name = {}
    for node in gltf["nodes"]:
        if "mesh" in node and "extras" in node:
            mesh_index = node["mesh"]
            node_by_geom_name[node["extras"]["name"]] = mesh_index

    materials = gltf["materials"]
    meshes = gltf["meshes"]

    for name, expected_color in EXPECTED_COLORS.items():
        mesh_index = node_by_geom_name[name]
        primitive = meshes[mesh_index]["primitives"][0]
        material_index = primitive["material"]
        base_color = materials[material_index]["pbrMetallicRoughness"]["baseColorFactor"]
        assert tuple(round(c, 2) for c in base_color[:3]) == tuple(round(c, 2) for c in expected_color)


def test_materials_are_not_metallic(tmp_path):
    """usd.py の create_materials は metallic=0.0 を明示設定するが、glTF書き出しが
    それを読み落とすと glTF仕様の既定値(metallicFactor省略時は1.0=完全な金属)が
    採用されてしまう。環境マップの無いシンプルな指向性/半球照明下では、金属材質は
    ディフューズ反射を持たないためbaseColorFactorが正しくてもほぼ真っ黒に描画され、
    実際にWebビューワーのE2Eテストでこの見た目のバグとして発見された
    （Issue #19）。metallicFactor/roughnessFactorがUSD側の値(0.0/1.0)を
    正しく引き継いでいることを回帰検証する。"""
    usda = tmp_path / "minimal.usda"
    convert(FIXTURE, usda)
    stage = Usd.Stage.Open(str(usda))

    out = tmp_path / "minimal.glb"
    export_gltf(stage, str(out))

    gltf = _gltf_json_from_glb(out)
    for material in gltf["materials"]:
        pbr = material["pbrMetallicRoughness"]
        assert pbr.get("metallicFactor", 1.0) == pytest.approx(0.0)
        assert pbr.get("roughnessFactor", 1.0) == pytest.approx(1.0)


def test_glb_primitives_have_normal_accessor(tmp_path):
    """法線がGLBに含まれる（POSITIONのみでNORMALが欠落しない）。"""
    usda = tmp_path / "minimal.usda"
    convert(FIXTURE, usda)
    stage = Usd.Stage.Open(str(usda))

    out = tmp_path / "minimal.glb"
    export_gltf(stage, str(out))

    gltf = _gltf_json_from_glb(out)
    for mesh in gltf["meshes"]:
        for primitive in mesh["primitives"]:
            assert "NORMAL" in primitive["attributes"]


def test_material_without_binding_falls_back_to_display_color(tmp_path):
    """マテリアル未バインドのメッシュは、灰色決め打ちではなくdisplayColorを使う。
    metallic/roughnessもUSD側の既定(0.0/1.0、非金属)にフォールバックする。"""
    from pxr import Gf

    from ifc2usd.gltf import _mesh_material_properties

    stage = Usd.Stage.CreateInMemory()
    mesh = UsdGeom.Mesh.Define(stage, "/Unmaterialed")
    mesh.CreatePointsAttr([(0, 0, 0), (1, 0, 0), (0, 1, 0)])
    mesh.CreateFaceVertexCountsAttr([3])
    mesh.CreateFaceVertexIndicesAttr([0, 1, 2])
    mesh.GetDisplayColorAttr().Set([Gf.Vec3f(0.1, 0.2, 0.3)])

    r, g, b, alpha, metallic, roughness = _mesh_material_properties(mesh, stage)
    assert (round(r, 3), round(g, 3), round(b, 3)) == (0.1, 0.2, 0.3)
    assert alpha == 1.0
    assert metallic == pytest.approx(0.0)
    assert roughness == pytest.approx(1.0)


def test_transparent_material_opacity_is_reflected_in_alpha(tmp_path):
    """opacityが1未満のマテリアル（例: IfcWindow相当）は、baseColorFactorのalphaと
    alphaMode=BLENDに反映される（1.0固定で無視されない）。"""
    from pxr import Gf, Sdf, UsdShade

    from ifc2usd.gltf import _mesh_material_properties

    stage = Usd.Stage.CreateInMemory()
    mat = UsdShade.Material.Define(stage, "/Materials/Glass")
    shader = UsdShade.Shader.Define(stage, "/Materials/Glass/PBRShader")
    shader.CreateIdAttr("UsdPreviewSurface")
    shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(0.6, 0.8, 0.9))
    shader.CreateInput("opacity", Sdf.ValueTypeNames.Float).Set(0.2)  # transparency=0.8相当
    mat.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(), "surface")

    mesh = UsdGeom.Mesh.Define(stage, "/Window")
    mesh.CreatePointsAttr([(0, 0, 0), (1, 0, 0), (0, 1, 0)])
    mesh.CreateFaceVertexCountsAttr([3])
    mesh.CreateFaceVertexIndicesAttr([0, 1, 2])
    UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim())
    UsdShade.MaterialBindingAPI(mesh).Bind(mat, UsdShade.Tokens.preview)

    r, g, b, alpha, _metallic, _roughness = _mesh_material_properties(mesh, stage)
    assert round(alpha, 2) == 0.2
    assert (round(r, 2), round(g, 2), round(b, 2)) == (0.6, 0.8, 0.9)


def test_bound_material_metallic_and_roughness_are_read_from_shader(tmp_path):
    """バインドされたUsdPreviewSurfaceのmetallic/roughness inputが、glTF側にも
    正しく引き継がれること（Issue #19で発見: 読み落とすとglTF仕様の既定値
    metallicFactor=1.0が採用され、環境マップ無しではほぼ真っ黒に描画される）。"""
    from pxr import Gf, Sdf, UsdShade

    from ifc2usd.gltf import _mesh_material_properties

    stage = Usd.Stage.CreateInMemory()
    mat = UsdShade.Material.Define(stage, "/Materials/Metal")
    shader = UsdShade.Shader.Define(stage, "/Materials/Metal/PBRShader")
    shader.CreateIdAttr("UsdPreviewSurface")
    shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(0.5, 0.5, 0.5))
    shader.CreateInput("metallic", Sdf.ValueTypeNames.Float).Set(0.9)
    shader.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(0.3)
    mat.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(), "surface")

    mesh = UsdGeom.Mesh.Define(stage, "/MetalThing")
    mesh.CreatePointsAttr([(0, 0, 0), (1, 0, 0), (0, 1, 0)])
    mesh.CreateFaceVertexCountsAttr([3])
    mesh.CreateFaceVertexIndicesAttr([0, 1, 2])
    UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim())
    UsdShade.MaterialBindingAPI(mesh).Bind(mat, UsdShade.Tokens.preview)

    _r, _g, _b, _alpha, metallic, roughness = _mesh_material_properties(mesh, stage)
    assert metallic == pytest.approx(0.9)
    assert roughness == pytest.approx(0.3)


def test_export_reflects_y_up_conversion(tmp_path):
    """--y-up変換後のUSDでも、GLBのワールドバウンディングボックスが妥当な範囲になる。"""
    usda = tmp_path / "minimal_yup.usda"
    convert(FIXTURE, usda, y_up=True)
    stage = Usd.Stage.Open(str(usda))

    out = tmp_path / "minimal_yup.glb"
    export_gltf(stage, str(out))

    reopened = trimesh.load(str(out))
    extents = reopened.bounds[1] - reopened.bounds[0]
    # Y-UPでも壁2枚分のワールドバウンディングボックスの大きさ自体は変わらない
    # (5.2 x 4.0 x 3.0 の並び替えのみ)
    assert sorted(round(e, 2) for e in extents) == sorted([5.2, 4.0, 3.0])
