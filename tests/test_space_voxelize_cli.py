"""`ifc2usd space-voxelize` サブコマンド（E9-5）のE2Eテスト。"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

import ifcopenshell
import ifcopenshell.api.aggregate
import ifcopenshell.api.context
import ifcopenshell.api.geometry
import ifcopenshell.api.project
import ifcopenshell.api.root
import ifcopenshell.api.spatial
import ifcopenshell.api.unit
import ifcopenshell.util.shape_builder

from ifc2usd import convert
from ifc2usd.cli import main
from ifc2usd.voxel import decode_morton_indices


def _build_ifc_with_wall_and_space(tmp_path) -> Path:
    """壁1枚+空間1つを持つIFC（`space-voxelize`が正本USDと空間の両方を必要と
    するため、`test_space_geometry.py`と違い壁も含める）。"""
    model = ifcopenshell.api.project.create_file(version="IFC4")
    project = ifcopenshell.api.root.create_entity(model, ifc_class="IfcProject", name="P")
    metre = ifcopenshell.api.unit.add_si_unit(model, unit_type="LENGTHUNIT")
    ifcopenshell.api.unit.assign_unit(model, units=[metre])

    context = ifcopenshell.api.context.add_context(model, context_type="Model")
    body = ifcopenshell.api.context.add_context(
        model, context_type="Model", context_identifier="Body", target_view="MODEL_VIEW", parent=context
    )

    site = ifcopenshell.api.root.create_entity(model, ifc_class="IfcSite", name="Site")
    building = ifcopenshell.api.root.create_entity(model, ifc_class="IfcBuilding", name="Building")
    storey = ifcopenshell.api.root.create_entity(model, ifc_class="IfcBuildingStorey", name="Storey")
    ifcopenshell.api.aggregate.assign_object(model, products=[site], relating_object=project)
    ifcopenshell.api.aggregate.assign_object(model, products=[building], relating_object=site)
    ifcopenshell.api.aggregate.assign_object(model, products=[storey], relating_object=building)

    builder = ifcopenshell.util.shape_builder.ShapeBuilder(model)

    wall = ifcopenshell.api.root.create_entity(model, ifc_class="IfcWall", name="Wall")
    wall_profile = builder.rectangle(size=np.array([4.0, 0.2]))
    wall_solid = builder.extrude(wall_profile, magnitude=3.0, position=np.array([0.0, 0.0, 0.0]))
    wall_representation = builder.get_representation(body, [wall_solid])
    ifcopenshell.api.geometry.assign_representation(model, product=wall, representation=wall_representation)
    ifcopenshell.api.spatial.assign_container(model, products=[wall], relating_structure=storey)

    space = ifcopenshell.api.root.create_entity(model, ifc_class="IfcSpace", name="Room 101")
    space_profile = builder.rectangle(size=np.array([4.0, 3.0]))
    space_solid = builder.extrude(space_profile, magnitude=3.0, position=np.array([0.0, 0.0, 0.0]))
    space_representation = builder.get_representation(body, [space_solid])
    ifcopenshell.api.geometry.assign_representation(model, product=space, representation=space_representation)
    ifcopenshell.api.aggregate.assign_object(model, products=[space], relating_object=storey)

    path = tmp_path / "wall_and_space.ifc"
    model.write(str(path))
    return path


def _build_ifc_with_space_below_reference_origin(tmp_path) -> Path:
    """壁のAABB最小点(z=0)より下（z=-1）まで空間ジオメトリが伸びているIFC
    ——`voxelize_mesh`の「originは各要素自身のAABB最小点以下」制約を破る、
    非典型的だが現実にありうる構成（コードレビューで検出：このケースが
    生のValueErrorで落ちずCLIの分かりやすいエラーになることの回帰テスト用）。"""
    model = ifcopenshell.api.project.create_file(version="IFC4")
    project = ifcopenshell.api.root.create_entity(model, ifc_class="IfcProject", name="P")
    metre = ifcopenshell.api.unit.add_si_unit(model, unit_type="LENGTHUNIT")
    ifcopenshell.api.unit.assign_unit(model, units=[metre])

    context = ifcopenshell.api.context.add_context(model, context_type="Model")
    body = ifcopenshell.api.context.add_context(
        model, context_type="Model", context_identifier="Body", target_view="MODEL_VIEW", parent=context
    )

    site = ifcopenshell.api.root.create_entity(model, ifc_class="IfcSite", name="Site")
    building = ifcopenshell.api.root.create_entity(model, ifc_class="IfcBuilding", name="Building")
    storey = ifcopenshell.api.root.create_entity(model, ifc_class="IfcBuildingStorey", name="Storey")
    ifcopenshell.api.aggregate.assign_object(model, products=[site], relating_object=project)
    ifcopenshell.api.aggregate.assign_object(model, products=[building], relating_object=site)
    ifcopenshell.api.aggregate.assign_object(model, products=[storey], relating_object=building)

    builder = ifcopenshell.util.shape_builder.ShapeBuilder(model)

    wall = ifcopenshell.api.root.create_entity(model, ifc_class="IfcWall", name="Wall")
    wall_profile = builder.rectangle(size=np.array([4.0, 0.2]))
    wall_solid = builder.extrude(wall_profile, magnitude=3.0, position=np.array([0.0, 0.0, 0.0]))
    wall_representation = builder.get_representation(body, [wall_solid])
    ifcopenshell.api.geometry.assign_representation(model, product=wall, representation=wall_representation)
    ifcopenshell.api.spatial.assign_container(model, products=[wall], relating_structure=storey)

    space = ifcopenshell.api.root.create_entity(model, ifc_class="IfcSpace", name="Basement Room")
    space_profile = builder.rectangle(size=np.array([4.0, 3.0]))
    # z=-1から始める: 壁(z=0起点)だけから求めたsceneOriginを下回る
    space_solid = builder.extrude(space_profile, magnitude=3.0, position=np.array([0.0, 0.0, -1.0]))
    space_representation = builder.get_representation(body, [space_solid])
    ifcopenshell.api.geometry.assign_representation(model, product=space, representation=space_representation)
    ifcopenshell.api.aggregate.assign_object(model, products=[space], relating_object=storey)

    path = tmp_path / "space_below_origin.ifc"
    model.write(str(path))
    return path


def test_space_voxelize_writes_space_voxels_json(tmp_path):
    ifc_path = _build_ifc_with_wall_and_space(tmp_path)
    usda = tmp_path / "model.usda"
    convert(ifc_path, usda)

    output = tmp_path / "space_voxels.json"
    exit_code = main(
        ["space-voxelize", str(ifc_path), "--reference", str(usda), "--size", "1.0", "-o", str(output)]
    )
    assert exit_code == 0
    assert output.is_file()

    data = json.loads(output.read_text())
    assert data["version"] == 3
    assert len(data["lods"]) == 1
    assert data["lods"][0]["size"] == 1.0
    [element] = data["lods"][0]["elements"]
    assert element["class"] == "IfcSpace"
    assert element["name"] == "Room 101"
    assert len(decode_morton_indices(element["indices"])) > 0


def test_space_voxelize_shares_origin_with_reference_voxels(tmp_path):
    """digital-twin-spec.md §5.4: シーン共有originはvoxels.jsonと同一規約
    （正本の全要素のAABB最小点であり、空間だけから独立に求めない）。"""
    ifc_path = _build_ifc_with_wall_and_space(tmp_path)
    usda = tmp_path / "model.usda"
    convert(ifc_path, usda)

    voxels_out = tmp_path / "voxels"
    main(["voxelize", str(usda), "--size", "1.0", "-o", str(voxels_out)])
    voxels_data = json.loads(voxels_out.with_suffix(".json").read_text())

    space_output = tmp_path / "space_voxels.json"
    main(["space-voxelize", str(ifc_path), "--reference", str(usda), "--size", "1.0", "-o", str(space_output)])
    space_data = json.loads(space_output.read_text())

    assert space_data["origin"] == voxels_data["origin"]


def test_space_voxelize_rejects_missing_ifc_file(tmp_path):
    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "space-voxelize",
                str(tmp_path / "does_not_exist.ifc"),
                "--reference",
                str(tmp_path / "irrelevant.usda"),
            ]
        )
    assert excinfo.value.code != 0


def test_space_voxelize_rejects_missing_reference(tmp_path):
    ifc_path = _build_ifc_with_wall_and_space(tmp_path)
    with pytest.raises(SystemExit) as excinfo:
        main(["space-voxelize", str(ifc_path), "--reference", str(tmp_path / "does_not_exist.usda")])
    assert excinfo.value.code != 0


def test_space_voxelize_reports_clean_error_when_space_extends_below_scene_origin(tmp_path):
    """コードレビューで検出: 空間ジオメトリが正本の共有originより下にはみ出す
    非典型的なモデルでも、生のValueErrorではなく分かりやすいCLIエラーで終了する。"""
    ifc_path = _build_ifc_with_space_below_reference_origin(tmp_path)
    usda = tmp_path / "model.usda"
    convert(ifc_path, usda)

    with pytest.raises(SystemExit) as excinfo:
        main(["space-voxelize", str(ifc_path), "--reference", str(usda), "--size", "1.0"])
    assert excinfo.value.code != 0
