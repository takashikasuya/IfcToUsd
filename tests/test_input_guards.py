"""外部入力に対するガード（E10-8 / Issue #65）の検証。

`export-gltf` / `serve` は「ifc2usd が書いた USD」以外を渡されうるし、IFC 側の
プロパティ集合も仕様どおりとは限らない。落ちずに、情報を失う場合は警告する。
"""

from __future__ import annotations

import logging

import ifcopenshell
import pytest
from pxr import Gf, Sdf, Usd, UsdGeom, UsdShade

from ifc2usd.gltf import export_gltf
from ifc2usd.ifc import get_properties


@pytest.fixture(autouse=True)
def _reset_property_warning_dedup():
    """警告の重複抑止はプロセス全体で共有される状態なので、テスト間で持ち越さない。"""
    from ifc2usd import ifc

    ifc._WARNED_PROPERTY_FAILURES.clear()
    yield
    ifc._WARNED_PROPERTY_FAILURES.clear()


def _stage_with_shader_named(shader_name: str | None) -> Usd.Stage:
    """メッシュ 1 枚 + 任意のシェーダ prim 名を持つ USD を組み立てる。"""
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    root = stage.DefinePrim("/IFC_Model", "Xform")
    stage.SetDefaultPrim(root)

    mat = UsdShade.Material.Define(stage, Sdf.Path("/Materials/M"))
    if shader_name is not None:
        shader = UsdShade.Shader.Define(stage, Sdf.Path(f"/Materials/M/{shader_name}"))
        shader.CreateIdAttr("UsdPreviewSurface")
        shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(0.1, 0.2, 0.3))
        mat.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(), "surface")

    element = UsdGeom.Xform.Define(stage, "/IFC_Model/Element_1")
    element.GetPrim().SetCustomDataByKey("GUID", "G1")
    element.GetPrim().SetCustomDataByKey("class", "IfcWall")

    mesh = UsdGeom.Mesh.Define(stage, "/IFC_Model/Element_1/mesh")
    mesh.CreatePointsAttr([(0, 0, 0), (1, 0, 0), (1, 1, 0)])
    mesh.CreateFaceVertexCountsAttr([3])
    mesh.CreateFaceVertexIndicesAttr([0, 1, 2])
    mesh.GetDisplayColorAttr().Set([Gf.Vec3f(0.9, 0.8, 0.7)])
    UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim())
    UsdShade.MaterialBindingAPI(mesh).Bind(mat, UsdShade.Tokens.preview)
    return stage


@pytest.mark.parametrize("shader_name", ["Surface", "preview_shader", None])
def test_export_gltf_survives_foreign_shader_naming(tmp_path, shader_name):
    stage = _stage_with_shader_named(shader_name)
    out = tmp_path / "foreign.glb"

    export_gltf(stage, str(out))

    assert out.exists() and out.stat().st_size > 0


def _model_with_quantities(quantity_specs):
    """指定した IfcQuantity* を並べた IfcElementQuantity を持つ壁の IFC を作る。"""
    model = ifcopenshell.file(schema="IFC4")
    owner = None
    wall = model.create_entity("IfcWall", GlobalId=ifcopenshell.guid.new(), Name="W")

    quantities = []
    for kind, name, value in quantity_specs:
        quantities.append(model.create_entity(kind, Name=name, **{_QUANTITY_ATTR[kind]: value}))

    qto = model.create_entity(
        "IfcElementQuantity", GlobalId=ifcopenshell.guid.new(), Name="Qto", Quantities=quantities or None
    )
    model.create_entity(
        "IfcRelDefinesByProperties",
        GlobalId=ifcopenshell.guid.new(),
        OwnerHistory=owner,
        RelatedObjects=[wall],
        RelatingPropertyDefinition=qto,
    )
    return wall


_QUANTITY_ATTR = {
    "IfcQuantityArea": "AreaValue",
    "IfcQuantityLength": "LengthValue",
    "IfcQuantityVolume": "VolumeValue",
}


def test_all_quantities_are_read_not_just_the_first():
    wall = _model_with_quantities(
        [
            ("IfcQuantityLength", "Width", 0.2),
            ("IfcQuantityArea", "Gross Side Area", 12.5),
            ("IfcQuantityVolume", "Net Volume", 2.5),
        ]
    )

    props = get_properties(wall)

    assert props["Gross_Side_Area"] == pytest.approx(12.5)
    assert props["Width"] == pytest.approx(0.2)
    assert props["Net_Volume"] == pytest.approx(2.5)


def test_enumerated_property_values_are_read_not_warned(caplog):
    """IfcPropertyEnumeratedValue は IFC2X3 の正当な型。警告ではなく値を読む。"""
    model = ifcopenshell.file(schema="IFC4")
    wall = model.create_entity("IfcWall", GlobalId=ifcopenshell.guid.new(), Name="W")
    prop = model.create_entity(
        "IfcPropertyEnumeratedValue",
        Name="Shape",
        EnumerationValues=[model.create_entity("IfcLabel", "RECTANGULAR")],
    )
    pset = model.create_entity(
        "IfcPropertySet", GlobalId=ifcopenshell.guid.new(), Name="Pset", HasProperties=[prop]
    )
    model.create_entity(
        "IfcRelDefinesByProperties",
        GlobalId=ifcopenshell.guid.new(),
        RelatedObjects=[wall],
        RelatingPropertyDefinition=pset,
    )

    with caplog.at_level(logging.WARNING, logger="ifc2usd.ifc"):
        props = get_properties(wall)

    assert props["Shape"] == "RECTANGULAR"
    assert caplog.records == []


def test_repeated_property_failures_warn_only_once(caplog):
    """同種の不正プロパティが数千件あってもログを埋め尽くさない。"""
    model = ifcopenshell.file(schema="IFC4")
    walls = []
    for _ in range(5):
        wall = model.create_entity("IfcWall", GlobalId=ifcopenshell.guid.new(), Name="W")
        prop = model.create_entity("IfcPropertySingleValue", Name="Broken", NominalValue=None)
        pset = model.create_entity(
            "IfcPropertySet", GlobalId=ifcopenshell.guid.new(), Name="Pset", HasProperties=[prop]
        )
        model.create_entity(
            "IfcRelDefinesByProperties",
            GlobalId=ifcopenshell.guid.new(),
            RelatedObjects=[wall],
            RelatingPropertyDefinition=pset,
        )
        walls.append(wall)

    with caplog.at_level(logging.WARNING, logger="ifc2usd.ifc"):
        for wall in walls:
            get_properties(wall)

    assert len(caplog.records) == 1


def test_property_extraction_failures_are_warned_not_swallowed(caplog):
    model = ifcopenshell.file(schema="IFC4")
    wall = model.create_entity("IfcWall", GlobalId=ifcopenshell.guid.new(), Name="W")
    # NominalValue を持たない不正なプロパティ
    prop = model.create_entity("IfcPropertySingleValue", Name="Broken", NominalValue=None)
    pset = model.create_entity(
        "IfcPropertySet", GlobalId=ifcopenshell.guid.new(), Name="Pset", HasProperties=[prop]
    )
    model.create_entity(
        "IfcRelDefinesByProperties",
        GlobalId=ifcopenshell.guid.new(),
        RelatedObjects=[wall],
        RelatingPropertyDefinition=pset,
    )

    with caplog.at_level(logging.WARNING, logger="ifc2usd.ifc"):
        props = get_properties(wall)

    assert "Broken" not in props
    assert any("Broken" in r.message for r in caplog.records)
