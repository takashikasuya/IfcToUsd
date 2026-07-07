"""ボクセル JSON（現行 v3）ライター（`docs/viewer/spec.md` §2）のテスト。

`tests/fixtures/minimal.ifc` の壁2枚から `VoxelElement` を組み立て、
`build_voxel_json` の出力をスキーマ・座標整合性・ソート順・色形式について検証する。
"""

from __future__ import annotations

import pytest
from pxr import Usd, UsdGeom

from ifc2usd.voxel import (
    VoxelElement,
    build_voxel_json,
    decode_morton_indices,
    morton_decode,
    scene_origin,
)
from tests.conftest import mesh_diffuse_color, wall_mesh_path, world_mesh

WALL_DIMS = {
    "Wall North": (5.0, 0.2, 3.0),
    "Wall East": (0.2, 4.0, 3.0),
}


def _elements(stage: Usd.Stage) -> list[VoxelElement]:
    elements = []
    for prim in stage.Traverse():
        cd = prim.GetCustomData()
        if cd.get("class") != "IfcWall":
            continue
        mesh_path = str(prim.GetPath().AppendChild("mesh"))
        vertices, indices = world_mesh(stage, mesh_path)
        color = mesh_diffuse_color(stage, mesh_path)
        elements.append(
            VoxelElement(
                guid=cd["GUID"],
                cls=cd["class"],
                name=cd.get("Name"),
                color=color,
                vertices=vertices,
                indices=indices,
            )
        )
    return elements


def _scene_world_min(stage: Usd.Stage) -> tuple[float, float, float]:
    bbox = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_])
    aligned = bbox.ComputeWorldBound(stage.GetDefaultPrim()).ComputeAlignedRange()
    return tuple(aligned.GetMin())


def test_schema_shape(stage):
    elements = _elements(stage)
    result = build_voxel_json(elements, sizes=[0.5], source={"usd": "minimal.usda"})

    assert result["version"] == 3
    assert result["units"] == "m"
    assert result["upAxis"] == "Z"
    assert result["source"] == {"usd": "minimal.usda"}
    assert len(result["origin"]) == 3
    assert isinstance(result["lods"], list) and len(result["lods"]) == 1
    lod = result["lods"][0]
    assert lod["size"] == 0.5
    assert {el["guid"] for el in lod["elements"]} == {e.guid for e in elements}
    for el in lod["elements"]:
        assert set(el) == {"guid", "class", "name", "color", "indices"}


def test_origin_matches_scene_world_aabb_min(stage):
    elements = _elements(stage)
    result = build_voxel_json(elements, sizes=[0.5])

    expected_min = _scene_world_min(stage)
    assert tuple(round(v, 6) for v in result["origin"]) == tuple(round(v, 6) for v in expected_min)


def test_voxel_centers_decode_within_element_world_bounds(stage):
    size = 0.5
    elements = _elements(stage)
    result = build_voxel_json(elements, sizes=[size])
    origin = result["origin"]

    element_bounds = {}
    for el in elements:
        xs = [v[0] for v in el.vertices]
        ys = [v[1] for v in el.vertices]
        zs = [v[2] for v in el.vertices]
        element_bounds[el.guid] = (min(xs), min(ys), min(zs), max(xs), max(ys), max(zs))

    lod = result["lods"][0]
    for el in lod["elements"]:
        xmin, ymin, zmin, xmax, ymax, zmax = element_bounds[el["guid"]]
        for code in decode_morton_indices(el["indices"]):
            ix, iy, iz = morton_decode(code)
            cx = origin[0] + (ix + 0.5) * size
            cy = origin[1] + (iy + 0.5) * size
            cz = origin[2] + (iz + 0.5) * size
            # ボクセル中心は要素のAABBから半ボクセル分の余裕を持って収まる
            tol = size / 2 + 1e-6
            assert xmin - tol <= cx <= xmax + tol
            assert ymin - tol <= cy <= ymax + tol
            assert zmin - tol <= cz <= zmax + tol


def test_indices_are_sorted(stage):
    elements = _elements(stage)
    result = build_voxel_json(elements, sizes=[0.5, 0.25])

    for lod in result["lods"]:
        for el in lod["elements"]:
            codes = decode_morton_indices(el["indices"])
            assert codes == sorted(codes)
            assert len(codes) == len(set(codes))  # 重複なし


def test_duplicate_sizes_produce_single_lod(stage):
    elements = _elements(stage)
    result = build_voxel_json(elements, sizes=[0.5, 0.5, 0.25, 0.5])

    assert [lod["size"] for lod in result["lods"]] == [0.5, 0.25]


def test_color_is_plain_rgb_not_morton_encoded(stage):
    elements = _elements(stage)
    result = build_voxel_json(elements, sizes=[0.5])

    by_name = {el["name"]: el for el in result["lods"][0]["elements"]}
    north = by_name["Wall North"]
    assert tuple(round(c, 3) for c in north["color"]) == (0.8, 0.2, 0.2)
    east = by_name["Wall East"]
    assert tuple(round(c, 3) for c in east["color"]) == (0.2, 0.5, 0.8)


def test_scene_origin_rejects_non_finite_vertices(stage):
    """1要素のNaN頂点が他の正常な要素のoriginまで汚染しないことを保証する。"""
    elements = _elements(stage)
    bad_vertices = [(float("nan"), 0.0, 0.0)] + list(elements[0].vertices[1:])
    poisoned = elements[0]._replace(vertices=bad_vertices)

    with pytest.raises(ValueError):
        scene_origin([poisoned, elements[1]])


def test_element_with_zero_voxels_is_kept_with_empty_indices(stage, monkeypatch):
    """あるLODで占有ボクセルが0個になっても、要素自体をindices=[]で残す
    （他のLODには出現するのに黙って消えると、ビューワー側で「このLODに
    存在しない」のか「存在するが空」なのか区別できなくなるため）。"""
    import ifc2usd.voxel as voxel_module

    elements = _elements(stage)
    real_voxelize_mesh = voxel_module.voxelize_mesh

    def fake_voxelize_mesh(vertices, indices, size, origin=None, fill=False):
        used_origin, voxels = real_voxelize_mesh(vertices, indices, size, origin=origin, fill=fill)
        return used_origin, set()  # 常に0ボクセルを返す

    monkeypatch.setattr(voxel_module, "voxelize_mesh", fake_voxelize_mesh)

    result = build_voxel_json(elements, sizes=[0.5])
    guids = {el["guid"] for el in result["lods"][0]["elements"]}
    assert guids == {e.guid for e in elements}
    for el in result["lods"][0]["elements"]:
        assert decode_morton_indices(el["indices"]) == []


def test_multiple_lods_have_independent_voxel_counts(stage):
    elements = _elements(stage)
    result = build_voxel_json(elements, sizes=[0.5, 0.25])

    sizes = [lod["size"] for lod in result["lods"]]
    assert sizes == [0.5, 0.25]

    counts_by_size = {
        lod["size"]: sum(len(decode_morton_indices(el["indices"])) for el in lod["elements"])
        for lod in result["lods"]
    }
    # より細かい格子のほうがボクセル総数は多い
    assert counts_by_size[0.25] > counts_by_size[0.5]


def test_indices_encoding_significantly_reduces_json_size_for_large_element():
    """Issue #38 / E7-4 の受け入れ条件（大規模モデルでJSON出力サイズが有意に
    削減される）を、素朴な整数リストとの直接比較で計測する。格子座標から直接
    組み立てた2種類の大規模ボクセル集合（このテストの関心はJSONサイズであって
    ボクセル化アルゴリズム自体の正しさではないため、`voxelize_mesh`は経由しない）
    で計測する:

    - 中実な立方体（`fill=True`の壁・床・スラブ等に相当）: 走査線内の連続セルが
      同じdeltaの長いrunになるため、delta+RLEは劇的に効く（実測: 40^3セルで
      約99.8%削減）。
    - 表面シェルのみ（既定の`fill=False`、薄い外殻）: runは短くなるが、
      それでも実測で約20%以上削減される（実測値73%に対し閾値80%と、格子サイズ
      や符号化アルゴリズムの微修正で崩れないよう十分な余裕を持たせている）。
    """
    import json

    from ifc2usd.voxel import encode_morton_indices, morton_encode

    n = 40  # 1辺40セル(64,000ボクセル格子)

    solid = sorted(morton_encode(ix, iy, iz) for ix in range(n) for iy in range(n) for iz in range(n))
    solid_naive = len(json.dumps(solid))
    solid_encoded = len(json.dumps(encode_morton_indices(solid)))
    assert solid_encoded < solid_naive * 0.05  # 実測 ~0.2%

    shell = sorted(
        morton_encode(ix, iy, iz)
        for ix in range(n)
        for iy in range(n)
        for iz in range(n)
        if ix in (0, n - 1) or iy in (0, n - 1) or iz in (0, n - 1)
    )
    shell_naive = len(json.dumps(shell))
    shell_encoded = len(json.dumps(encode_morton_indices(shell)))
    assert shell_encoded < shell_naive * 0.80  # 実測 ~73%（余裕を持たせた閾値）
