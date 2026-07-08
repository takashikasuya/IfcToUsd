"""`ifc2usd/space_heatmap.py`（空間ボクセルヒートマップ集計、E9-5）のテスト。"""

from __future__ import annotations

import numpy as np
import pytest
import trimesh

from ifc2usd.space_heatmap import (
    aggregate_values_by_space,
    aggregate_values_by_storey,
    build_space_voxel_index,
    build_space_voxel_json,
)
from ifc2usd.voxel import VoxelElement, decode_morton_indices, morton_encode


def _box(extents, translation) -> tuple[list, list]:
    """`tests/test_voxel.py`の`_duplicated_vertex_box`と同じ、面ごとに独立した
    頂点を持つ（weld-vertices=Falseを模した）ボックスメッシュ。"""
    box = trimesh.creation.box(extents=extents)
    box.apply_translation(translation)
    verts = box.vertices[box.faces].reshape(-1, 3)
    indices = np.arange(len(verts))
    return verts.tolist(), indices.tolist()


def _space_element(guid, extents, translation, name="Room") -> VoxelElement:
    verts, indices = _box(extents, translation)
    return VoxelElement(guid=guid, cls="IfcSpace", name=name, color=(0.5, 0.5, 0.5), vertices=verts, indices=indices)


# --- build_space_voxel_index ---


def test_build_space_voxel_index_assigns_non_overlapping_spaces_independently():
    space_a = _space_element("space-a", extents=[2, 2, 2], translation=[1, 1, 1])  # world (0,0,0)-(2,2,2)
    space_b = _space_element("space-b", extents=[2, 2, 2], translation=[3, 1, 1])  # world (2,0,0)-(4,2,2)

    index = build_space_voxel_index([space_a, space_b], size=1.0, origin=(0.0, 0.0, 0.0))

    assigned_guids = set(index.values())
    assert assigned_guids == {"space-a", "space-b"}
    assert index[morton_encode(0, 0, 0)] == "space-a"
    assert index[morton_encode(2, 0, 0)] == "space-b"


def test_build_space_voxel_index_prefers_smaller_space_on_overlap():
    """隣接空間の境界セルは体積の小さい方（充填ボクセル数が少ない方）へ帰属する。"""
    big = _space_element("big", extents=[3, 3, 3], translation=[1.5, 1.5, 1.5])  # world (0,0,0)-(3,3,3), 27cells
    small = _space_element("small", extents=[2, 2, 2], translation=[3, 3, 3])  # world (2,2,2)-(4,4,4), 8cells

    index = build_space_voxel_index([big, small], size=1.0, origin=(0.0, 0.0, 0.0))

    # (2,2,2)はbig([0,2]範囲)とsmall([2,3]範囲)の両方に属しうる共有セル
    assert index[morton_encode(2, 2, 2)] == "small"
    # 明確にbig側だけの領域は引き続きbigに属する
    assert index[morton_encode(0, 0, 0)] == "big"
    # 明確にsmall側だけの領域は引き続きsmallに属する
    assert index[morton_encode(3, 3, 3)] == "small"


def test_build_space_voxel_index_prefers_smaller_space_regardless_of_input_order():
    """コードレビューで検出: 勝敗が「小さい方」ではなく「入力リストの後ろにある方」
    に依存していないことを、順序を反転させても確認する。"""
    big = _space_element("big", extents=[3, 3, 3], translation=[1.5, 1.5, 1.5])
    small = _space_element("small", extents=[2, 2, 2], translation=[3, 3, 3])

    index = build_space_voxel_index([small, big], size=1.0, origin=(0.0, 0.0, 0.0))

    assert index[morton_encode(2, 2, 2)] == "small"


def test_build_space_voxel_index_skips_elements_without_vertices():
    empty = VoxelElement(guid="empty", cls="IfcSpace", name=None, color=(0, 0, 0), vertices=[], indices=[])
    space = _space_element("space-a", extents=[1, 1, 1], translation=[0.5, 0.5, 0.5])

    index = build_space_voxel_index([empty, space], size=1.0, origin=(0.0, 0.0, 0.0))

    assert set(index.values()) == {"space-a"}


# --- build_space_voxel_json ---


def test_build_space_voxel_json_schema():
    space_a = _space_element("space-a", extents=[1, 1, 1], translation=[0.5, 0.5, 0.5], name="Room A")

    result = build_space_voxel_json([space_a], sizes=[1.0], origin=(0.0, 0.0, 0.0), up_axis="Z")

    assert result["version"] == 3
    assert result["units"] == "m"
    assert result["upAxis"] == "Z"
    assert result["origin"] == [0.0, 0.0, 0.0]
    assert len(result["lods"]) == 1
    [element] = result["lods"][0]["elements"]
    assert element["guid"] == "space-a"
    assert element["class"] == "IfcSpace"
    assert element["name"] == "Room A"
    assert decode_morton_indices(element["indices"]) == [morton_encode(0, 0, 0)]


def test_build_space_voxel_json_excludes_overlap_cell_from_losing_space():
    """build_voxel_jsonと違い各要素を独立に再ボクセル化しないため、境界セルが
    2つの空間のindicesへ二重に出力されない。"""
    big = _space_element("big", extents=[3, 3, 3], translation=[1.5, 1.5, 1.5])
    small = _space_element("small", extents=[2, 2, 2], translation=[3, 3, 3])

    result = build_space_voxel_json([big, small], sizes=[1.0], origin=(0.0, 0.0, 0.0))

    elements_by_guid = {el["guid"]: el for el in result["lods"][0]["elements"]}
    big_codes = decode_morton_indices(elements_by_guid["big"]["indices"])
    small_codes = decode_morton_indices(elements_by_guid["small"]["indices"])

    shared_code = morton_encode(2, 2, 2)
    assert shared_code not in big_codes
    assert shared_code in small_codes
    assert set(big_codes) & set(small_codes) == set()


def test_build_space_voxel_json_empty_space_has_empty_indices():
    empty = VoxelElement(guid="empty", cls="IfcSpace", name=None, color=(0, 0, 0), vertices=[], indices=[])

    result = build_space_voxel_json([empty], sizes=[1.0], origin=(0.0, 0.0, 0.0))

    [element] = result["lods"][0]["elements"]
    assert decode_morton_indices(element["indices"]) == []


# --- aggregate_values_by_space ---


def test_aggregate_values_by_space_mean_is_default():
    entries = [
        {"spaceGuid": "room-1", "value": 20.0, "unit": "celsius"},
        {"spaceGuid": "room-1", "value": 24.0, "unit": "celsius"},
        {"spaceGuid": "room-2", "value": 30.0, "unit": "celsius"},
    ]

    result = aggregate_values_by_space(entries)

    assert result["room-1"] == {"value": 22.0, "count": 2, "unit": "celsius"}
    assert result["room-2"] == {"value": 30.0, "count": 1, "unit": "celsius"}


@pytest.mark.parametrize(
    "aggregation,expected",
    [("min", 20.0), ("max", 24.0), ("count", 2)],
)
def test_aggregate_values_by_space_supports_min_max_count(aggregation, expected):
    entries = [
        {"spaceGuid": "room-1", "value": 20.0, "unit": "celsius"},
        {"spaceGuid": "room-1", "value": 24.0, "unit": "celsius"},
    ]

    result = aggregate_values_by_space(entries, aggregation=aggregation)

    assert result["room-1"]["value"] == expected


def test_aggregate_values_by_space_ignores_entries_without_space_guid():
    entries = [
        {"guid": "element-guid-only", "value": 99.0},
        {"spaceGuid": "room-1", "value": 20.0},
    ]

    result = aggregate_values_by_space(entries)

    assert set(result) == {"room-1"}


def test_aggregate_values_by_space_rejects_unknown_aggregation():
    with pytest.raises(ValueError):
        aggregate_values_by_space([{"spaceGuid": "room-1", "value": 1.0}], aggregation="median")


def test_aggregate_values_by_space_no_numeric_values_is_none():
    entries = [{"spaceGuid": "room-1", "value": None}]
    result = aggregate_values_by_space(entries)
    assert result["room-1"] == {"value": None, "count": 0, "unit": None}


def test_aggregate_values_by_space_excludes_booleans():
    """コードレビューで検出: boolはintのサブクラスのため、真偽値のメトリックが
    誤って1/0として数値集計に混入しないことを確認する。"""
    entries = [{"spaceGuid": "room-1", "value": True}, {"spaceGuid": "room-1", "value": False}]
    result = aggregate_values_by_space(entries)
    assert result["room-1"] == {"value": None, "count": 0, "unit": None}


# --- aggregate_values_by_storey (フォールバック) ---


def test_aggregate_values_by_storey_groups_via_mapping():
    entries = [
        {"guid": "elem-a", "value": 20.0, "unit": "celsius"},
        {"guid": "elem-b", "value": 24.0, "unit": "celsius"},
        {"guid": "elem-c", "value": 30.0, "unit": "celsius"},
    ]
    guid_to_storey = {"elem-a": "storey-1", "elem-b": "storey-1", "elem-c": "storey-2"}

    result = aggregate_values_by_storey(entries, guid_to_storey)

    assert result["storey-1"] == {"value": 22.0, "count": 2, "unit": "celsius"}
    assert result["storey-2"] == {"value": 30.0, "count": 1, "unit": "celsius"}


def test_aggregate_values_by_storey_ignores_unmapped_guids():
    entries = [{"guid": "unknown-elem", "value": 20.0}]
    result = aggregate_values_by_storey(entries, {})
    assert result == {}
