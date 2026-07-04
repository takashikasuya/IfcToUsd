"""IFC → USD 変換のエンドツーエンドテスト。

`tests/fixtures/minimal.ifc`（`generate_fixture.py` で生成）を変換し、
出力 USD の座標系・階層・ジオメトリ・マテリアルを検証する。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pxr import Usd, UsdGeom, UsdShade

from ifc2usd import convert

FIXTURE = Path(__file__).parent / "fixtures" / "minimal.ifc"

# フィクスチャの壁: 名前 -> (期待する displayColor, 頂点数)
EXPECTED_WALLS = {
    "Wall North": ((0.8, 0.2, 0.2), 24),
    "Wall East": ((0.2, 0.5, 0.8), 24),
}


@pytest.fixture(scope="module")
def stage(tmp_path_factory) -> Usd.Stage:
    out = tmp_path_factory.mktemp("usd") / "minimal.usda"
    convert(FIXTURE, out)
    return Usd.Stage.Open(str(out))


def test_stage_metadata(stage):
    assert UsdGeom.GetStageUpAxis(stage) == UsdGeom.Tokens.z
    assert UsdGeom.GetStageMetersPerUnit(stage) == 1.0
    root = stage.GetDefaultPrim()
    assert root.GetPath() == "/IFC_Model"
    assert Usd.ModelAPI(root).GetKind() == "assembly"


def test_counts_and_hierarchy(stage):
    meshes = [p for p in stage.Traverse() if p.IsA(UsdGeom.Mesh)]
    materials = [p for p in stage.Traverse() if p.IsA(UsdShade.Material)]
    assert len(meshes) == 2
    assert len(materials) == 2
    for mesh in meshes:
        # Site → Building → Storey → Element → mesh の階層に置かれている
        assert str(mesh.GetPath()).startswith("/IFC_Model/Site/Building/Storey_")
        assert mesh.GetName() == "mesh"


def test_world_extent(stage):
    bbox = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_])
    aligned = bbox.ComputeWorldBound(stage.GetDefaultPrim()).ComputeAlignedRange()
    # 2枚の壁の合成: x[0,5.2], y[0,4], z[0,3]（メートル）
    assert tuple(round(v, 3) for v in aligned.GetMin()) == (0.0, 0.0, 0.0)
    assert tuple(round(v, 3) for v in aligned.GetMax()) == (5.2, 4.0, 3.0)


def test_wall_colors_and_binding(stage):
    seen = {}
    for prim in stage.Traverse():
        cd = prim.GetCustomData()
        if cd.get("class") != "IfcWall":
            continue
        name = cd.get("Name")
        mesh = UsdGeom.Mesh(stage.GetPrimAtPath(prim.GetPath().AppendChild("mesh")))
        display = tuple(round(c, 3) for c in mesh.GetDisplayColorAttr().Get()[0])
        mat_path = UsdShade.MaterialBindingAPI(mesh).GetDirectBinding().GetMaterialPath()
        shader = UsdShade.Shader(stage.GetPrimAtPath(mat_path.AppendChild("PBRShader")))
        diffuse = tuple(round(c, 3) for c in shader.GetInput("diffuseColor").Get())
        n_points = len(mesh.GetPointsAttr().Get())
        seen[name] = (display, diffuse, n_points)

    assert set(seen) == set(EXPECTED_WALLS)
    for name, (color, n_points) in EXPECTED_WALLS.items():
        display, diffuse, points = seen[name]
        assert display == color
        assert diffuse == color
        assert points == n_points


def test_custom_data_present(stage):
    site = stage.GetPrimAtPath("/IFC_Model/Site")
    cd = site.GetCustomData()
    assert cd.get("class") == "IfcSite"
    assert "GUID" in cd
