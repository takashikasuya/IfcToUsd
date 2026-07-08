"""`ifc.py::get_space_geometry()`（E9-5の先行タスク、IfcSpace抽出経路）のテスト。

`tests/fixtures/minimal.ifc`にはIfcSpaceが無いため、専用の最小フィクスチャを
その場で構築する（`generate_fixture.py`の壁作成と同じifcopenshell 0.8 API、
Spaceは`IsDecomposedBy`でstoreyへ集約する点だけが壁と異なる——`usd.py`の
`build_stage`がSite/Building/Storey/Spaceをその関係で辿るのに合わせる）。
"""

from __future__ import annotations

import numpy as np
import pytest

import ifcopenshell
import ifcopenshell.api.aggregate
import ifcopenshell.api.context
import ifcopenshell.api.geometry
import ifcopenshell.api.project
import ifcopenshell.api.root
import ifcopenshell.api.unit
import ifcopenshell.util.shape_builder

from ifc2usd.ifc import create_settings, get_geometry, get_space_geometry


def _build_ifc_with_space(space_origin=(1.0, 2.0, 0.0), space_size=(4.0, 3.0, 3.0), name="Room 101"):
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

    space = ifcopenshell.api.root.create_entity(model, ifc_class="IfcSpace", name=name)
    builder = ifcopenshell.util.shape_builder.ShapeBuilder(model)
    profile = builder.rectangle(size=np.array([space_size[0], space_size[1]]))
    solid = builder.extrude(profile, magnitude=space_size[2], position=np.array(space_origin, dtype=float))
    representation = builder.get_representation(body, [solid])
    ifcopenshell.api.geometry.assign_representation(model, product=space, representation=representation)
    ifcopenshell.api.aggregate.assign_object(model, products=[space], relating_object=storey)

    return model, space


@pytest.fixture
def space_ifc_path(tmp_path):
    model, space = _build_ifc_with_space()
    path = tmp_path / "space.ifc"
    model.write(str(path))
    return path, space.GlobalId


def test_get_space_geometry_yields_world_space_vertices(space_ifc_path):
    path, guid = space_ifc_path
    ifc_file = ifcopenshell.open(str(path))
    settings = create_settings()

    results = list(get_space_geometry(settings, ifc_file))

    assert len(results) == 1
    result_guid, name, verts, indices = results[0]
    assert result_guid == guid
    assert name == "Room 101"
    assert len(verts) > 0
    assert len(indices) > 0 and len(indices) % 3 == 0

    verts_arr = np.asarray(verts)
    np.testing.assert_allclose(verts_arr.min(axis=0), [1.0, 2.0, 0.0], atol=1e-6)
    np.testing.assert_allclose(verts_arr.max(axis=0), [5.0, 5.0, 3.0], atol=1e-6)


def test_get_space_geometry_applies_y_up_swap(space_ifc_path):
    path, _ = space_ifc_path
    ifc_file = ifcopenshell.open(str(path))
    settings = create_settings()

    [(_, _, verts, _)] = list(get_space_geometry(settings, ifc_file, y_up=True))

    verts_arr = np.asarray(verts)
    # Y-UPではY/Zが入れ替わる: 元のZ範囲[0,3]がYに、元のY範囲[2,5]がZに来る。
    np.testing.assert_allclose(verts_arr.min(axis=0), [1.0, 0.0, 2.0], atol=1e-6)
    np.testing.assert_allclose(verts_arr.max(axis=0), [5.0, 3.0, 5.0], atol=1e-6)


def test_get_space_geometry_yields_nothing_without_spaces(tmp_path):
    """空間を含まないIFC（既存のminimal.ifc相当）ではジェネレータは何も生成しない。"""
    from tests.generate_fixture import build

    model = build()
    path = tmp_path / "no_space.ifc"
    model.write(str(path))
    ifc_file = ifcopenshell.open(str(path))
    settings = create_settings()

    assert list(get_space_geometry(settings, ifc_file)) == []


def test_get_geometry_still_excludes_spaces(space_ifc_path):
    """既存のget_geometry()（正本USD/GLBの生成経路）は引き続きIfcSpaceを除外する
    ——空間ボクセルヒートマップは付加的アセットとして完全に独立させる設計。"""
    path, guid = space_ifc_path
    ifc_file = ifcopenshell.open(str(path))
    settings = create_settings()
    materials: dict = {}

    guids_seen = []
    for verts, indices, norms, info, material, color, matrix in get_geometry(settings, ifc_file, materials):
        guids_seen.append(info["GlobalId"])

    assert guid not in guids_seen
