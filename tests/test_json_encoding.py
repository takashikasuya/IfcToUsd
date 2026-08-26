"""CLI が書き出す JSON の文字エンコーディング（E10-8 / Issue #65）。

JSON は UTF-8 でなければならない（RFC 8259）。`Path.write_text` は encoding を
省略するとロケール既定（日本語 Windows なら cp932）になり、非ASCIIの要素名を
含むモデルでビューワーの `fetch` + `JSON.parse` が壊れる。実データ
（files/kawasaki-model.ifc, 日本語の要素名を含む）で実際に踏んだ不具合。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from ifc2usd.cli import main
from ifc2usd.usd import build_stage
from ifc2usd.ifc import GeometryData, MaterialSpec

NON_ASCII_NAME = "樋_縦樋_ver2"


@pytest.fixture
def usd_with_non_ascii_names(tmp_path):
    """日本語の要素名・マテリアル名を持つ最小の USD を組み立てる。"""
    import ifcopenshell
    import ifcopenshell.api.aggregate
    import ifcopenshell.api.context
    import ifcopenshell.api.geometry
    import ifcopenshell.api.project
    import ifcopenshell.api.root
    import ifcopenshell.api.spatial
    import ifcopenshell.api.style
    import ifcopenshell.api.unit
    import ifcopenshell.util.shape_builder

    from ifc2usd import convert

    model = ifcopenshell.api.project.create_file(version="IFC4")
    project = ifcopenshell.api.root.create_entity(model, ifc_class="IfcProject", name="P")
    ifcopenshell.api.unit.assign_unit(
        model, units=[ifcopenshell.api.unit.add_si_unit(model, unit_type="LENGTHUNIT")]
    )
    context = ifcopenshell.api.context.add_context(model, context_type="Model")
    body = ifcopenshell.api.context.add_context(
        model, context_type="Model", context_identifier="Body", target_view="MODEL_VIEW", parent=context
    )
    site = ifcopenshell.api.root.create_entity(model, ifc_class="IfcSite", name="敷地")
    building = ifcopenshell.api.root.create_entity(model, ifc_class="IfcBuilding", name="建物")
    storey = ifcopenshell.api.root.create_entity(model, ifc_class="IfcBuildingStorey", name="1階")
    ifcopenshell.api.aggregate.assign_object(model, products=[site], relating_object=project)
    ifcopenshell.api.aggregate.assign_object(model, products=[building], relating_object=site)
    ifcopenshell.api.aggregate.assign_object(model, products=[storey], relating_object=building)

    builder = ifcopenshell.util.shape_builder.ShapeBuilder(model)
    wall = ifcopenshell.api.root.create_entity(model, ifc_class="IfcWall", name=NON_ASCII_NAME)
    profile = builder.rectangle(size=np.array([2.0, 0.3]))
    solid = builder.extrude(profile, magnitude=2.0, position=np.array([0.0, 0.0, 0.0]))
    representation = builder.get_representation(body, [solid])
    ifcopenshell.api.geometry.assign_representation(model, product=wall, representation=representation)
    ifcopenshell.api.spatial.assign_container(model, products=[wall], relating_structure=storey)

    ifc_path = tmp_path / "japanese.ifc"
    model.write(str(ifc_path))
    usda = tmp_path / "japanese.usda"
    convert(ifc_path, usda)
    return usda


def test_voxelize_json_is_utf8(tmp_path, usd_with_non_ascii_names):
    out_base = tmp_path / "voxels"
    assert main(["voxelize", str(usd_with_non_ascii_names), "--size", "1.0", "-o", str(out_base)]) == 0

    json_path = out_base.with_suffix(".json")
    raw = json_path.read_bytes()
    assert NON_ASCII_NAME.encode("utf-8") in raw, "非ASCII名が UTF-8 で書かれていない"

    data = json.loads(raw.decode("utf-8"))
    names = {el["name"] for el in data["lods"][0]["elements"]}
    assert NON_ASCII_NAME in names
