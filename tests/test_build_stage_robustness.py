"""`build_stage` の縮退モデル耐性（E10-1 / Issue #58）の検証。

Site 欠落・複数 Site/Building・1 要素に複数の分解関係・空間階層に属さない要素、
といった実データで起こりうる構成で、変換が壊れず情報も落とさないことを確認する。
"""

from __future__ import annotations

import logging

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
from pxr import Usd

from ifc2usd import convert


def _new_model():
    model = ifcopenshell.api.project.create_file(version="IFC4")
    ifcopenshell.api.root.create_entity(model, ifc_class="IfcProject", name="P")
    metre = ifcopenshell.api.unit.add_si_unit(model, unit_type="LENGTHUNIT")
    ifcopenshell.api.unit.assign_unit(model, units=[metre])
    context = ifcopenshell.api.context.add_context(model, context_type="Model")
    body = ifcopenshell.api.context.add_context(
        model, context_type="Model", context_identifier="Body", target_view="MODEL_VIEW", parent=context
    )
    return model, body, ifcopenshell.util.shape_builder.ShapeBuilder(model)


def _add_wall(model, body, builder, name, origin):
    wall = ifcopenshell.api.root.create_entity(model, ifc_class="IfcWall", name=name)
    profile = builder.rectangle(size=np.array([1.0, 1.0]))
    solid = builder.extrude(profile, magnitude=1.0, position=np.array(origin, dtype=float))
    representation = builder.get_representation(body, [solid])
    ifcopenshell.api.geometry.assign_representation(model, product=wall, representation=representation)
    return wall


def _convert(model, tmp_path, name):
    ifc_path = tmp_path / f"{name}.ifc"
    model.write(str(ifc_path))
    out = tmp_path / f"{name}.usda"
    convert(ifc_path, out)
    return Usd.Stage.Open(str(out))


def _prim_paths_with_class(stage, cls):
    return [str(p.GetPath()) for p in stage.Traverse() if p.GetCustomData().get("class") == cls]


def test_multiple_sites_and_buildings_keep_their_own_storeys(tmp_path):
    model, body, builder = _new_model()
    project = model.by_type("IfcProject")[0]
    for i in range(2):
        site = ifcopenshell.api.root.create_entity(model, ifc_class="IfcSite", name=f"Site{i}")
        building = ifcopenshell.api.root.create_entity(model, ifc_class="IfcBuilding", name=f"Bldg{i}")
        storey = ifcopenshell.api.root.create_entity(model, ifc_class="IfcBuildingStorey", name=f"Storey{i}")
        ifcopenshell.api.aggregate.assign_object(model, products=[site], relating_object=project)
        ifcopenshell.api.aggregate.assign_object(model, products=[building], relating_object=site)
        ifcopenshell.api.aggregate.assign_object(model, products=[storey], relating_object=building)
        wall = _add_wall(model, body, builder, f"Wall{i}", (float(i) * 3.0, 0.0, 0.0))
        ifcopenshell.api.spatial.assign_container(model, products=[wall], relating_structure=storey)

    stage = _convert(model, tmp_path, "multi")

    storey_paths = _prim_paths_with_class(stage, "IfcBuildingStorey")
    assert len(storey_paths) == 2
    # 各 Storey は自分の Building 配下にあり、片方へ寄せられていない
    assert len({p.rsplit("/", 1)[0] for p in storey_paths}) == 2
    assert len(_prim_paths_with_class(stage, "IfcWall")) == 2


def test_model_without_a_site_still_converts(tmp_path, caplog):
    model, body, builder = _new_model()
    project = model.by_type("IfcProject")[0]
    building = ifcopenshell.api.root.create_entity(model, ifc_class="IfcBuilding", name="Bldg")
    storey = ifcopenshell.api.root.create_entity(model, ifc_class="IfcBuildingStorey", name="Storey")
    ifcopenshell.api.aggregate.assign_object(model, products=[building], relating_object=project)
    ifcopenshell.api.aggregate.assign_object(model, products=[storey], relating_object=building)
    wall = _add_wall(model, body, builder, "Wall", (0.0, 0.0, 0.0))
    ifcopenshell.api.spatial.assign_container(model, products=[wall], relating_structure=storey)

    with caplog.at_level(logging.WARNING, logger="ifc2usd.usd"):
        stage = _convert(model, tmp_path, "nosite")

    assert len(_prim_paths_with_class(stage, "IfcWall")) == 1
    assert any("IfcSite" in r.message for r in caplog.records)


def test_multiple_decomposition_relations_are_all_processed(tmp_path):
    model, body, builder = _new_model()
    project = model.by_type("IfcProject")[0]
    site = ifcopenshell.api.root.create_entity(model, ifc_class="IfcSite", name="Site")
    building = ifcopenshell.api.root.create_entity(model, ifc_class="IfcBuilding", name="Bldg")
    storey = ifcopenshell.api.root.create_entity(model, ifc_class="IfcBuildingStorey", name="Storey")
    ifcopenshell.api.aggregate.assign_object(model, products=[site], relating_object=project)
    ifcopenshell.api.aggregate.assign_object(model, products=[building], relating_object=site)
    ifcopenshell.api.aggregate.assign_object(model, products=[storey], relating_object=building)

    spaces = []
    for i in range(2):
        space = ifcopenshell.api.root.create_entity(model, ifc_class="IfcSpace", name=f"Space{i}")
        spaces.append(space)
        # API を介さず個別の IfcRelAggregates を作り、分解関係を意図的に 2 本にする
        model.create_entity(
            "IfcRelAggregates",
            GlobalId=ifcopenshell.guid.new(),
            RelatingObject=storey,
            RelatedObjects=[space],
        )

    stage = _convert(model, tmp_path, "multidecomp")

    assert len(_prim_paths_with_class(stage, "IfcSpace")) == len(spaces)


def test_element_outside_the_spatial_tree_is_reported(tmp_path, caplog):
    model, body, builder = _new_model()
    project = model.by_type("IfcProject")[0]
    site = ifcopenshell.api.root.create_entity(model, ifc_class="IfcSite", name="Site")
    building = ifcopenshell.api.root.create_entity(model, ifc_class="IfcBuilding", name="Bldg")
    storey = ifcopenshell.api.root.create_entity(model, ifc_class="IfcBuildingStorey", name="Storey")
    ifcopenshell.api.aggregate.assign_object(model, products=[site], relating_object=project)
    ifcopenshell.api.aggregate.assign_object(model, products=[building], relating_object=site)
    ifcopenshell.api.aggregate.assign_object(model, products=[storey], relating_object=building)

    placed = _add_wall(model, body, builder, "Placed", (0.0, 0.0, 0.0))
    ifcopenshell.api.spatial.assign_container(model, products=[placed], relating_structure=storey)
    orphan = _add_wall(model, body, builder, "Orphan", (5.0, 0.0, 0.0))

    with caplog.at_level(logging.WARNING, logger="ifc2usd.usd"):
        stage = _convert(model, tmp_path, "orphan")

    assert len(_prim_paths_with_class(stage, "IfcWall")) == 1
    assert any(orphan.GlobalId in r.message or "1" in r.message for r in caplog.records)
    assert any("配置" in r.message or "unplaced" in r.message.lower() for r in caplog.records)


@pytest.mark.parametrize("ifc_class", ["IfcSite", "IfcBuilding"])
def test_single_site_and_building_keep_the_legacy_prim_names(tmp_path, ifc_class):
    model, body, builder = _new_model()
    project = model.by_type("IfcProject")[0]
    site = ifcopenshell.api.root.create_entity(model, ifc_class="IfcSite", name="Site")
    building = ifcopenshell.api.root.create_entity(model, ifc_class="IfcBuilding", name="Bldg")
    ifcopenshell.api.aggregate.assign_object(model, products=[site], relating_object=project)
    ifcopenshell.api.aggregate.assign_object(model, products=[building], relating_object=site)

    stage = _convert(model, tmp_path, "legacy")

    assert _prim_paths_with_class(stage, ifc_class) == [
        "/IFC_Model/Site" if ifc_class == "IfcSite" else "/IFC_Model/Site/Building"
    ]
