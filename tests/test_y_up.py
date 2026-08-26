"""`--y-up` の座標変換（E10-3 / Issue #60）の検証。

従来実装は頂点の Y/Z を入れ替えていたが、これは行列式 −1 の鏡像変換であり
0° ヨーでも左右が反転する。正しくは X 軸まわり −90° の回転
``(x, y, z) -> (x, z, -y)`` をルートに 1 回だけ適用する。
"""

from __future__ import annotations

from pathlib import Path

import ifcopenshell
import ifcopenshell.api.aggregate
import ifcopenshell.api.context
import ifcopenshell.api.geometry
import ifcopenshell.api.project
import ifcopenshell.api.root
import ifcopenshell.api.spatial
import ifcopenshell.api.unit
import ifcopenshell.util.shape_builder
import numpy as np
import pytest
from pxr import Gf, Usd, UsdGeom

from ifc2usd import convert
from ifc2usd.usd import MESH_PRIM_NAME

FIXTURE = Path(__file__).parent / "fixtures" / "minimal.ifc"


def zup_to_yup(p):
    """Z-UP 座標を Y-UP へ持ち上げる（X 軸まわり −90°）。"""
    return (p[0], p[2], -p[1])


def _world_points(stage) -> dict[str, list[tuple[float, float, float]]]:
    points: dict[str, list[tuple[float, float, float]]] = {}
    for prim in stage.Traverse():
        if prim.GetName() != MESH_PRIM_NAME:
            continue
        xform = UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(Usd.TimeCode.Default())
        raw = UsdGeom.Mesh(prim).GetPointsAttr().Get() or []
        points[str(prim.GetPath())] = [tuple(xform.Transform(Gf.Vec3d(*p))) for p in raw]
    return points


def _convert_both(ifc_path: Path, tmp_path: Path):
    z_out = tmp_path / "zup.usda"
    y_out = tmp_path / "yup.usda"
    convert(ifc_path, z_out, y_up=False)
    convert(ifc_path, y_out, y_up=True)
    return Usd.Stage.Open(str(z_out)), Usd.Stage.Open(str(y_out))


def _yawed_wall_ifc(tmp_path: Path) -> Path:
    """45° ヨーで配置した壁を 1 枚だけ持つ IFC。鏡像バグと回転合成バグの両方を突く。"""
    model = ifcopenshell.api.project.create_file(version="IFC4")
    project = ifcopenshell.api.root.create_entity(model, ifc_class="IfcProject", name="P")
    metre = ifcopenshell.api.unit.add_si_unit(model, unit_type="LENGTHUNIT")
    ifcopenshell.api.unit.assign_unit(model, units=[metre])
    context = ifcopenshell.api.context.add_context(model, context_type="Model")
    body = ifcopenshell.api.context.add_context(
        model, context_type="Model", context_identifier="Body", target_view="MODEL_VIEW", parent=context
    )
    site = ifcopenshell.api.root.create_entity(model, ifc_class="IfcSite", name="Site")
    building = ifcopenshell.api.root.create_entity(model, ifc_class="IfcBuilding", name="B")
    storey = ifcopenshell.api.root.create_entity(model, ifc_class="IfcBuildingStorey", name="S")
    ifcopenshell.api.aggregate.assign_object(model, products=[site], relating_object=project)
    ifcopenshell.api.aggregate.assign_object(model, products=[building], relating_object=site)
    ifcopenshell.api.aggregate.assign_object(model, products=[storey], relating_object=building)

    builder = ifcopenshell.util.shape_builder.ShapeBuilder(model)
    wall = ifcopenshell.api.root.create_entity(model, ifc_class="IfcWall", name="Yawed")
    profile = builder.rectangle(size=np.array([4.0, 0.3]))
    solid = builder.extrude(profile, magnitude=2.5, position=np.array([0.0, 0.0, 0.0]))
    representation = builder.get_representation(body, [solid])
    ifcopenshell.api.geometry.assign_representation(model, product=wall, representation=representation)
    ifcopenshell.api.spatial.assign_container(model, products=[wall], relating_structure=storey)

    angle = np.pi / 4
    matrix = np.eye(4)
    matrix[:3, :3] = [
        [np.cos(angle), -np.sin(angle), 0.0],
        [np.sin(angle), np.cos(angle), 0.0],
        [0.0, 0.0, 1.0],
    ]
    matrix[:3, 3] = [1.0, 2.0, 0.0]
    ifcopenshell.api.geometry.edit_object_placement(model, product=wall, matrix=matrix)

    path = tmp_path / "yawed.ifc"
    model.write(str(path))
    return path


def test_stage_up_axis_is_y(tmp_path):
    _, y_stage = _convert_both(FIXTURE, tmp_path)
    assert UsdGeom.GetStageUpAxis(y_stage) == UsdGeom.Tokens.y


@pytest.mark.parametrize("model_name", ["fixture", "yawed"])
def test_y_up_world_points_are_a_rotation_of_z_up(tmp_path, model_name):
    ifc_path = FIXTURE if model_name == "fixture" else _yawed_wall_ifc(tmp_path)
    z_stage, y_stage = _convert_both(ifc_path, tmp_path)

    z_points = _world_points(z_stage)
    y_points = _world_points(y_stage)
    assert z_points and set(z_points) == set(y_points)

    for path, zs in z_points.items():
        expected = np.array([zup_to_yup(p) for p in zs])
        np.testing.assert_allclose(np.array(y_points[path]), expected, atol=1e-6)


def test_y_up_preserves_handedness(tmp_path):
    """鏡像変換だと三角形の巻き方向が反転する。回転なら符号付き体積の符号が保たれる。"""
    ifc_path = _yawed_wall_ifc(tmp_path)
    z_stage, y_stage = _convert_both(ifc_path, tmp_path)

    def signed_volume(stage):
        total = 0.0
        for prim in stage.Traverse():
            if prim.GetName() != MESH_PRIM_NAME:
                continue
            mesh = UsdGeom.Mesh(prim)
            xform = UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(Usd.TimeCode.Default())
            pts = np.array([tuple(xform.Transform(Gf.Vec3d(*p))) for p in mesh.GetPointsAttr().Get()])
            idx = np.array(mesh.GetFaceVertexIndicesAttr().Get()).reshape(-1, 3)
            a, b, c = pts[idx[:, 0]], pts[idx[:, 1]], pts[idx[:, 2]]
            total += float(np.sum(np.einsum("ij,ij->i", a, np.cross(b, c))) / 6.0)
        return total

    z_volume = signed_volume(z_stage)
    assert z_volume != pytest.approx(0.0, abs=1e-9)
    assert np.sign(signed_volume(y_stage)) == np.sign(z_volume)
