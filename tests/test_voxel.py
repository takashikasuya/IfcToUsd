"""voxel.py（メッシュ→表面占有ボクセル化 + Morton符号化）のテスト。

`tests/fixtures/minimal.ifc` の壁2枚を変換した実際のUSDメッシュに対して
ボクセル化を検証する。各壁は薄い（1ボクセル未満の厚み）軸並行ボックスなので、
表面占有ボクセル化は「厚み方向の前後面が断面全体を覆う」ため solid fill と
一致し、期待ボクセル数を解析的に計算できる
（nx = ceil(dim_x/size), ny = ceil(dim_y/size), nz = ceil(dim_z/size)）。
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest
from pxr import Gf, Usd, UsdGeom

from ifc2usd import convert
from ifc2usd.voxel import morton_decode, morton_encode, voxelize_mesh

FIXTURE = Path(__file__).parent / "fixtures" / "minimal.ifc"


def _world_mesh(stage: Usd.Stage, mesh_path: str):
    """USD メッシュの points をワールド座標へ変換し、(vertices, indices) を返す。"""
    prim = stage.GetPrimAtPath(mesh_path)
    mesh = UsdGeom.Mesh(prim)
    xform = UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(Usd.TimeCode.Default())
    points = [xform.Transform(Gf.Vec3d(*p)) for p in mesh.GetPointsAttr().Get()]
    vertices = [(p[0], p[1], p[2]) for p in points]
    indices = list(mesh.GetFaceVertexIndicesAttr().Get())
    return vertices, indices


@pytest.fixture(scope="module")
def stage(tmp_path_factory) -> Usd.Stage:
    out = tmp_path_factory.mktemp("usd") / "minimal.usda"
    convert(FIXTURE, out)
    return Usd.Stage.Open(str(out))


def _wall_mesh_path(stage: Usd.Stage, name: str) -> str:
    for prim in stage.Traverse():
        cd = prim.GetCustomData()
        if cd.get("class") == "IfcWall" and cd.get("Name") == name:
            return str(prim.GetPath().AppendChild("mesh"))
    raise AssertionError(f"wall not found: {name}")


# --- Morton (Z-order) 符号化 ---


def test_morton_round_trip():
    for x, y, z in [(0, 0, 0), (1, 2, 3), (10, 0, 5), (2097151, 2097151, 2097151), (0, 1, 0)]:
        code = morton_encode(x, y, z)
        assert morton_decode(code) == (x, y, z)


def test_morton_encode_is_injective_over_small_range():
    seen = set()
    for x in range(6):
        for y in range(6):
            for z in range(6):
                code = morton_encode(x, y, z)
                assert code not in seen
                seen.add(code)


def test_morton_rejects_negative_coordinates():
    with pytest.raises(ValueError):
        morton_encode(-1, 0, 0)


# --- 表面占有ボクセル化 ---


def test_voxelize_thin_wall_matches_analytic_box_count(stage):
    """薄い壁（厚み<size）は前後面が断面全体を覆うため fill と一致し、
    nx*ny*nz を解析的に計算できる。"""
    mesh_path = _wall_mesh_path(stage, "Wall North")
    vertices, indices = _world_mesh(stage, mesh_path)
    size = 0.5

    origin, voxels = voxelize_mesh(vertices, indices, size)

    dims = (5.0, 0.2, 3.0)  # generate_fixture.py の Wall North 寸法
    nx, ny, nz = (math.ceil(d / size) for d in dims)
    assert len(voxels) == nx * ny * nz

    xs = [v[0] for v in voxels]
    ys = [v[1] for v in voxels]
    zs = [v[2] for v in voxels]
    assert (min(xs), max(xs)) == (0, nx - 1)
    assert (min(ys), max(ys)) == (0, ny - 1)
    assert (min(zs), max(zs)) == (0, nz - 1)

    # origin + index*size がワールド座標のAABB最小点と一致する（自動算出時）
    assert tuple(round(v, 6) for v in origin) == (0.0, 0.0, 0.0)


def test_voxelize_with_shared_origin_offsets_indices(stage):
    """壁2枚を共有originでボクセル化すると、東壁は原点からのオフセット分だけ
    グリッドインデックスがずれる（JSON v2でのシーン共有originを想定）。"""
    size = 0.5
    shared_origin = (0.0, 0.0, 0.0)

    north_vertices, north_indices = _world_mesh(stage, _wall_mesh_path(stage, "Wall North"))
    east_vertices, east_indices = _world_mesh(stage, _wall_mesh_path(stage, "Wall East"))

    _, north_voxels = voxelize_mesh(north_vertices, north_indices, size, origin=shared_origin)
    _, east_voxels = voxelize_mesh(east_vertices, east_indices, size, origin=shared_origin)

    # Wall East は x=5.0 起点、寸法 (0.2, 4.0, 3.0)
    east_xs = {v[0] for v in east_voxels}
    assert east_xs == {round(5.0 / size)}  # ix = floor(5.0/0.5) = 10

    dims = (0.2, 4.0, 3.0)
    ny, nz = (math.ceil(d / size) for d in dims[1:])
    assert len(east_voxels) == ny * nz

    # 2枚の壁のボクセルは重ならない
    assert north_voxels.isdisjoint(east_voxels)


def test_voxelize_fill_mode_matches_surface_for_thin_wall(stage):
    """薄い壁は内部空洞がないため、fill=Trueでも表面占有と同じ結果になる。"""
    mesh_path = _wall_mesh_path(stage, "Wall North")
    vertices, indices = _world_mesh(stage, mesh_path)
    size = 0.5

    _, surface_voxels = voxelize_mesh(vertices, indices, size)
    _, fill_voxels = voxelize_mesh(vertices, indices, size, fill=True)

    assert fill_voxels == surface_voxels
