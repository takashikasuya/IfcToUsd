"""sdf_slice.py（要素ごとのSDF水平スライスJSON化）のテスト（Issue #29 / E5-3）。"""

from __future__ import annotations

import numpy as np
import pytest
import trimesh

from ifc2usd.sdf_slice import build_sdf_slices_json
from ifc2usd.voxel import VoxelElement
from tests.conftest import mesh_diffuse_color, wall_mesh_path, world_mesh


def _wall_element(stage, name: str) -> VoxelElement:
    mesh_path = wall_mesh_path(stage, name)
    vertices, indices = world_mesh(stage, mesh_path)
    color = mesh_diffuse_color(stage, mesh_path)
    prim = stage.GetPrimAtPath(mesh_path).GetParent()
    cd = prim.GetCustomData()
    return VoxelElement(
        guid=cd["GUID"], cls=cd["class"], name=cd.get("Name"),
        color=color, vertices=vertices, indices=indices,
    )


def _duplicated_vertex_box(extents, translation) -> tuple[list, list]:
    """weld-vertices=False を模した、面ごとに独立した頂点を持つボックスメッシュ
    （tests/test_voxel.py の同名ヘルパーと同じ構成）。"""
    box = trimesh.creation.box(extents=extents)
    box.apply_translation(translation)
    verts = box.vertices[box.faces].reshape(-1, 3)
    indices = np.arange(len(verts))
    return verts.tolist(), indices.tolist()


def _box_element(guid: str, extents, translation) -> VoxelElement:
    vertices, indices = _duplicated_vertex_box(extents, translation)
    return VoxelElement(
        guid=guid, cls="TestBox", name=guid, color=(1.0, 1.0, 1.0),
        vertices=vertices, indices=indices,
    )


def test_element_without_vertices_is_skipped():
    empty = VoxelElement(guid="empty", cls="X", name=None, color=(1, 1, 1), vertices=[], indices=[])
    result = build_sdf_slices_json([empty], size=0.5)
    assert result["elements"] == {}


def test_returns_grid_for_each_wall(stage):
    elements = [_wall_element(stage, "Wall North"), _wall_element(stage, "Wall East")]
    result = build_sdf_slices_json(elements, size=0.5, slice_count=3)

    assert result["version"] == 1
    assert result["size"] == 0.5
    for el in elements:
        entry = result["elements"][el.guid]
        assert entry["cols"] > 0
        assert entry["rows"] > 0
        assert 1 <= len(entry["slices"]) <= 3
        for sl in entry["slices"]:
            assert len(sl["values"]) == entry["rows"]
            assert all(len(row) == entry["cols"] for row in sl["values"])


def test_slice_count_capped_by_available_z_range(stage):
    """Wall North(高さ3m)をsize=2.0でボクセル化すると鉛直方向は2セルしかないため、
    slice_count=100を要求しても実際のスライス数はそれを超えない。"""
    el = _wall_element(stage, "Wall North")
    result = build_sdf_slices_json([el], size=2.0, slice_count=100)

    entry = result["elements"][el.guid]
    assert len(entry["slices"]) <= 2


def test_grid_aligned_cube_has_zero_surface_and_negative_interior():
    """1m立方体を0.25m格子で処理すると中心2x2x2セルが内部になる
    （tests/test_voxel.py の test_voxelize_fill_of_grid_aligned_cube_fills_interior 相当）。
    その中心高さのスライスは、立方体断面の縁が0、内部が負値になるはず。"""
    el = _box_element("cube", [1.0, 1.0, 1.0], [0.5, 0.5, 0.5])
    size = 0.25

    result = build_sdf_slices_json([el], size=size, slice_count=4, band_width=2)
    entry = result["elements"]["cube"]

    # 4x4x4グリッド、断面は4x4。
    assert entry["cols"] == 4
    assert entry["rows"] == 4

    # いずれかのスライスの中心2x2に負値（内部）が現れるはず。
    found_negative = any(
        any(v is not None and v < 0 for row in sl["values"] for v in row)
        for sl in entry["slices"]
    )
    assert found_negative

    # いずれかのスライスの外周セルに0（表面）が現れるはず。
    found_zero = any(
        any(v is not None and abs(v) < 1e-9 for row in sl["values"] for v in row)
        for sl in entry["slices"]
    )
    assert found_zero


def test_origin_matches_element_bbox_not_scene_origin():
    """originX/Yは要素自身のXYバウンディングボックス最小点（ボクセル境界に丸めた値）
    であるべきで、複数要素間で共有されるシーン全体originとは異なってよい。"""
    el = _box_element("cube", [1.0, 1.0, 1.0], [10.0, 20.0, 0.5])
    size = 0.5

    result = build_sdf_slices_json([el], size=size, slice_count=2)
    entry = result["elements"]["cube"]

    assert entry["originX"] == pytest.approx(9.5, abs=1e-6)
    assert entry["originY"] == pytest.approx(19.5, abs=1e-6)
