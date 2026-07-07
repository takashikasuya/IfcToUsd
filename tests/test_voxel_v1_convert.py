"""v1ボクセルJSON（ノートブック形式）→現行スキーマ変換のテスト（Issue #17 / E1-5）。

v1はGLTF_to_Voxel.ipynbのcell 6が出力する形式:

    {
      "voxelSize": 0.5,
      "offset": [ix, iy, iz],          # floor(ワールド最小点/voxelSize)の整数格子座標
      "elements": [
        {"guid": ..., "name": ..., "class": ...,
         "indices": [morton...],       # offset起点の格子インデックスのMorton符号
         "color": <int>,               # pymorton.interleave3(R, G, B)、各0-255
         "metadata": {...}}
      ]
    }

受け入れ条件に言及されるIfcOpenHouse.json自体はリポジトリに存在しないため、
ノートブックの出力コードと同一の形式を持つ合成フィクスチャで検証する。
Mortonのビット順は、pymorton.interleave3(x,y,z)がxを最下位ビットに置く規約で、
ifc2usd.voxel.morton_encodeと同一（tests側でも同じ関数で符号化して作る）。
"""

from __future__ import annotations

import pytest

from ifc2usd.voxel import convert_v1_voxel_json, decode_morton_indices, morton_decode, morton_encode


def _v1_fixture() -> dict:
    # 色: R=204, G=51, B=51 (v2の[0.8, 0.2, 0.2]相当)
    red_morton = morton_encode(204, 51, 51)
    blue_morton = morton_encode(51, 128, 204)
    return {
        "voxelSize": 0.5,
        "offset": [-2, 0, 3],
        "elements": [
            {
                "guid": "20FpTZCqJy2vhVJYtjuIce",
                "name": "壁-001",
                "class": "IfcWall",
                "indices": [morton_encode(0, 0, 0), morton_encode(1, 0, 0), morton_encode(0, 2, 1)],
                "color": red_morton,
                "metadata": {"Description": "test wall"},
            },
            {
                "guid": "3xYzAbCdEfGhIjKlMnOpQr",
                "name": "No Name",
                "class": "IfcSlab",
                "indices": [morton_encode(5, 5, 0)],
                "color": blue_morton,
                "metadata": {},
            },
        ],
    }


def test_converted_top_level_schema_matches_current_version():
    v2 = convert_v1_voxel_json(_v1_fixture())
    assert v2["version"] == 3
    assert v2["units"] == "m"
    assert v2["upAxis"] == "Z"
    assert len(v2["lods"]) == 1
    assert v2["lods"][0]["size"] == 0.5


def test_origin_is_offset_times_voxel_size_in_world_coordinates():
    """v1のoffsetは「floor(ワールド最小点/voxelSize)」の整数格子座標なので、
    v2のorigin(ワールド座標, m)へは voxelSize倍で変換する。"""
    v2 = convert_v1_voxel_json(_v1_fixture())
    assert v2["origin"] == [-2 * 0.5, 0 * 0.5, 3 * 0.5]


def test_morton_encoded_color_is_decoded_to_normalized_rgb():
    v2 = convert_v1_voxel_json(_v1_fixture())
    wall = v2["lods"][0]["elements"][0]
    assert wall["color"] == pytest.approx([204 / 255, 51 / 255, 51 / 255])

    slab = v2["lods"][0]["elements"][1]
    assert slab["color"] == pytest.approx([51 / 255, 128 / 255, 204 / 255])


def test_indices_are_preserved_and_sorted():
    """v1のindicesはoffset起点格子のMorton符号で、v2のorigin起点格子と同一の
    格子を指す(originはoffsetのワールド座標化に過ぎない)ため、値は変換不要。
    ただしv2はソート済み格納を規定している(spec.md §2)ので整列する。"""
    v2 = convert_v1_voxel_json(_v1_fixture())
    wall = v2["lods"][0]["elements"][0]
    original = [morton_encode(0, 0, 0), morton_encode(1, 0, 0), morton_encode(0, 2, 1)]
    codes = decode_morton_indices(wall["indices"])
    assert codes == sorted(original)
    # 復元先の格子座標も同一集合
    assert {morton_decode(c) for c in codes} == {(0, 0, 0), (1, 0, 0), (0, 2, 1)}


def test_guid_class_name_are_carried_over():
    v2 = convert_v1_voxel_json(_v1_fixture())
    wall = v2["lods"][0]["elements"][0]
    assert wall["guid"] == "20FpTZCqJy2vhVJYtjuIce"
    assert wall["class"] == "IfcWall"
    assert wall["name"] == "壁-001"


def test_metadata_is_not_duplicated_into_converted_output():
    """spec.md §2: 属性詳細はJSONへ重複格納せず、GUIDでUSD/scene.json側を参照する。
    v1のmetadataはv2要素へ持ち込まない。"""
    v2 = convert_v1_voxel_json(_v1_fixture())
    for el in v2["lods"][0]["elements"]:
        assert "metadata" not in el


def test_up_axis_parameter_is_recorded():
    """v1にはupAxisが無い(ノートブックはglTFシーンをそのままボクセル化しており
    座標系はソース依存)ため、呼び出し側が指定できる。既定はこのリポジトリの
    標準であるZ。"""
    v2 = convert_v1_voxel_json(_v1_fixture(), up_axis="Y")
    assert v2["upAxis"] == "Y"


def test_source_note_mentions_v1_conversion():
    v2 = convert_v1_voxel_json(_v1_fixture())
    assert "v1" in str(v2.get("source", {}))


def test_rejects_json_without_v1_markers():
    """v2ファイル(voxelSize/offsetが無い)を誤って渡した場合は黙って壊れた出力を
    返さず、明確なエラーにする。"""
    with pytest.raises(ValueError):
        convert_v1_voxel_json({"version": 2, "origin": [0, 0, 0], "lods": []})


def test_result_is_loadable_by_elements_from_current_schema_consumers():
    """変換結果がv2の既存コンシューマ(viewer.jsのbuildVoxelLodsが読む形)と同じ
    構造キーを持つこと(既存のbuild_voxel_json出力と同じキー集合)。"""
    from ifc2usd.voxel import build_voxel_json, VoxelElement

    native_v2 = build_voxel_json(
        [
            VoxelElement(
                guid="g", cls="IfcWall", name="w", color=(1, 0, 0),
                vertices=[(0, 0, 0), (1, 0, 0), (0, 1, 0)], indices=[0, 1, 2],
            )
        ],
        sizes=[0.5],
    )
    converted = convert_v1_voxel_json(_v1_fixture())

    assert set(native_v2.keys()) == set(converted.keys())
    native_el = native_v2["lods"][0]["elements"][0]
    converted_el = converted["lods"][0]["elements"][0]
    assert set(native_el.keys()) == set(converted_el.keys())
