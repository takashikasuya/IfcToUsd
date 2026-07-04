"""ボクセル JSON v2 ライター（`docs/viewer/spec.md` §2）のテスト。

`tests/fixtures/minimal.ifc` の壁2枚から `VoxelElement` を組み立て、
`build_voxel_json` の出力をスキーマ・座標整合性・ソート順・色形式について検証する。
"""

from __future__ import annotations

from pxr import Usd, UsdGeom

from ifc2usd.voxel import VoxelElement, build_voxel_json, morton_decode
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

    assert result["version"] == 2
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
        for code in el["indices"]:
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
            assert el["indices"] == sorted(el["indices"])
            assert len(el["indices"]) == len(set(el["indices"]))  # 重複なし


def test_color_is_plain_rgb_not_morton_encoded(stage):
    elements = _elements(stage)
    result = build_voxel_json(elements, sizes=[0.5])

    by_name = {el["name"]: el for el in result["lods"][0]["elements"]}
    north = by_name["Wall North"]
    assert tuple(round(c, 3) for c in north["color"]) == (0.8, 0.2, 0.2)
    east = by_name["Wall East"]
    assert tuple(round(c, 3) for c in east["color"]) == (0.2, 0.5, 0.8)


def test_multiple_lods_have_independent_voxel_counts(stage):
    elements = _elements(stage)
    result = build_voxel_json(elements, sizes=[0.5, 0.25])

    sizes = [lod["size"] for lod in result["lods"]]
    assert sizes == [0.5, 0.25]

    counts_by_size = {
        lod["size"]: sum(len(el["indices"]) for el in lod["elements"]) for lod in result["lods"]
    }
    # より細かい格子のほうがボクセル総数は多い
    assert counts_by_size[0.25] > counts_by_size[0.5]
