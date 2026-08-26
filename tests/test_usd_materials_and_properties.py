"""`create_materials` / `get_properties` の直接ユニットテスト（E10-11 / Issue #68）。

これまで変換の E2E 越しにしか通っていなかった分岐（透過マテリアル、
`BuildingAddress` の畳み込み、内部キーの除去）を直接固定する。
"""

from __future__ import annotations

import ifcopenshell
import pytest
from pxr import Usd, UsdShade

from ifc2usd.ifc import MaterialSpec, get_properties
from ifc2usd.usd import PBR_SHADER_NAME, create_materials


def _shader(stage, name):
    return UsdShade.Shader(stage.GetPrimAtPath(f"/Materials/{name}/{PBR_SHADER_NAME}"))


def test_create_materials_defines_one_material_per_entry():
    stage = Usd.Stage.CreateInMemory()

    prims = create_materials(
        stage,
        {
            "Concrete": MaterialSpec((0.6, 0.6, 0.6), None),
            "Glass": MaterialSpec((0.2, 0.4, 0.9), 0.8),
        },
    )

    assert set(prims) == {"Concrete", "Glass"}
    assert _shader(stage, "Concrete").GetIdAttr().Get() == "UsdPreviewSurface"


def test_opaque_material_defaults_to_non_metal():
    stage = Usd.Stage.CreateInMemory()
    create_materials(stage, {"Concrete": MaterialSpec((0.6, 0.6, 0.6), None)})

    shader = _shader(stage, "Concrete")
    assert shader.GetInput("metallic").Get() == pytest.approx(0.0)
    assert shader.GetInput("roughness").Get() == pytest.approx(1.0)
    assert shader.GetInput("opacity") is None or not shader.GetInput("opacity")


def test_transparency_becomes_opacity_and_mirror_smooth():
    stage = Usd.Stage.CreateInMemory()
    create_materials(stage, {"Glass": MaterialSpec((0.2, 0.4, 0.9), 0.8)})

    shader = _shader(stage, "Glass")
    # IFC の transparency は UsdPreviewSurface の opacity へ反転して入る
    assert shader.GetInput("opacity").Get() == pytest.approx(0.2)
    assert shader.GetInput("roughness").Get() == pytest.approx(0.0)


def test_material_surface_output_is_connected():
    stage = Usd.Stage.CreateInMemory()
    prims = create_materials(stage, {"Concrete": MaterialSpec((0.6, 0.6, 0.6), None)})

    source = prims["Concrete"].ComputeSurfaceSource()
    shader = source[0] if isinstance(source, tuple) else source
    assert shader.GetPath().name == PBR_SHADER_NAME


def test_get_properties_drops_internal_keys_and_keeps_identity():
    model = ifcopenshell.file(schema="IFC4")
    wall = model.create_entity("IfcWall", GlobalId=ifcopenshell.guid.new(), Name="W1")

    props = get_properties(wall)

    assert props["GlobalId"] == wall.GlobalId
    assert props["Name"] == "W1"
    assert props["type"] == "IfcWall"
    for dropped in ("OwnerHistory", "Representation", "ObjectPlacement"):
        assert dropped not in props


def test_get_properties_flattens_building_address():
    model = ifcopenshell.file(schema="IFC4")
    address = model.create_entity("IfcPostalAddress", AddressLines=["1-1 Chiyoda", "Tokyo"])
    building = model.create_entity(
        "IfcBuilding", GlobalId=ifcopenshell.guid.new(), Name="B", BuildingAddress=address
    )

    props = get_properties(building)

    assert props["Address"] == "1-1 Chiyoda"
    assert "BuildingAddress" not in props


def test_get_properties_reads_a_property_set():
    model = ifcopenshell.file(schema="IFC4")
    wall = model.create_entity("IfcWall", GlobalId=ifcopenshell.guid.new(), Name="W")
    prop = model.create_entity(
        "IfcPropertySingleValue", Name="FireRating", NominalValue=model.create_entity("IfcLabel", "A60")
    )
    pset = model.create_entity(
        "IfcPropertySet", GlobalId=ifcopenshell.guid.new(), Name="Pset_WallCommon", HasProperties=[prop]
    )
    model.create_entity(
        "IfcRelDefinesByProperties",
        GlobalId=ifcopenshell.guid.new(),
        RelatedObjects=[wall],
        RelatingPropertyDefinition=pset,
    )

    assert get_properties(wall)["FireRating"] == "A60"
