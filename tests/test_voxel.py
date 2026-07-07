"""voxel.py（メッシュ→表面占有ボクセル化 + Morton符号化）のテスト。

`tests/fixtures/minimal.ifc` の壁2枚を変換した実際のUSDメッシュに対して
ボクセル化を検証する。各壁は薄い（1ボクセル未満の厚み）軸並行ボックスなので、
表面占有ボクセル化は「厚み方向の前後面が断面全体を覆う」ため solid fill と
一致し、期待ボクセル数を解析的に計算できる
（nx = ceil(dim_x/size), ny = ceil(dim_y/size), nz = ceil(dim_z/size)）。
"""

from __future__ import annotations

import math

import numpy as np
import pytest
import trimesh

from ifc2usd.voxel import morton_decode, morton_encode, voxelize_mesh
from tests.conftest import wall_mesh_path, world_mesh


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
    mesh_path = wall_mesh_path(stage, "Wall North")
    vertices, indices = world_mesh(stage, mesh_path)
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

    north_vertices, north_indices = world_mesh(stage, wall_mesh_path(stage, "Wall North"))
    east_vertices, east_indices = world_mesh(stage, wall_mesh_path(stage, "Wall East"))

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
    mesh_path = wall_mesh_path(stage, "Wall North")
    vertices, indices = world_mesh(stage, mesh_path)
    size = 0.5

    _, surface_voxels = voxelize_mesh(vertices, indices, size)
    _, fill_voxels = voxelize_mesh(vertices, indices, size, fill=True)

    assert fill_voxels == surface_voxels


# --- 格子サイズと寸法がちょうど整数倍で揃う退化ケース ---


def _duplicated_vertex_box(extents, translation) -> tuple[list, list]:
    """weld-vertices=False を模した、面ごとに独立した頂点を持つボックスメッシュ。"""
    box = trimesh.creation.box(extents=extents)
    box.apply_translation(translation)
    verts = box.vertices[box.faces].reshape(-1, 3)
    indices = np.arange(len(verts))
    return verts.tolist(), indices.tolist()


def test_voxelize_surface_of_grid_aligned_cube_is_not_empty():
    """全ての面がちょうど格子線上に乗る形状（例: 1m立方体を0.5m格子で処理）でも
    表面が消失しない（各面がメッシュ全体の下端/上端かどうかで一意に解決される）。"""
    vertices, indices = _duplicated_vertex_box([1.0, 1.0, 1.0], [0.5, 0.5, 0.5])
    size = 0.5

    _, surface = voxelize_mesh(vertices, indices, size)

    # 1m立方体を0.5m格子（2x2x2=8セル）で処理すると、全セルが境界面に触れる
    assert len(surface) == 8
    assert {ix for ix, _, _ in surface} == {0, 1}
    assert {iy for _, iy, _ in surface} == {0, 1}
    assert {iz for _, _, iz in surface} == {0, 1}


def test_voxelize_fill_of_grid_aligned_cube_fills_interior():
    vertices, indices = _duplicated_vertex_box([1.0, 1.0, 1.0], [0.5, 0.5, 0.5])
    size = 0.25  # 4x4x4=64セル、中心2x2x2=8セルが内部

    _, surface = voxelize_mesh(vertices, indices, size)
    _, filled = voxelize_mesh(vertices, indices, size, fill=True)

    assert len(surface) == 64 - 8  # 中空シェル
    assert len(filled) == 64  # 内部充填で全セル


# --- Issue #36 (E7-2): 非watertightメッシュへの頑健化 ---


def _open_box_missing_one_face(extents, translation) -> tuple[list, list]:
    """weld-vertices=False を模しつつ、上面（法線 +Z）の2三角形を除去した
    真に穴（境界エッジ）を持つ箱。"""
    box = trimesh.creation.box(extents=extents)
    box.apply_translation(translation)
    keep = ~np.all(np.isclose(box.face_normals, [0.0, 0.0, 1.0]), axis=1)
    faces = box.faces[keep]
    verts = box.vertices[faces].reshape(-1, 3)
    indices = np.arange(len(verts))
    return verts.tolist(), indices.tolist()


def test_voxelize_fill_is_robust_to_touching_non_manifold_bodies():
    """接触する2つの立方体（weld-vertices=False で頂点非共有のため、接触面が
    二重に存在し非多様体エッジとなる形状）でも、フラッドフィルは各立方体の
    内部を正しく充填する。実データ(files/ToyodaLab.ifc)のIfcFurnishingElement/
    IfcBuildingElementProxyで見られる、複数ボディが接触する非多様体形状の
    再現（真の穴＝境界エッジは持たない）。trimeshのcontains()によるレイキャスト
    parity判定は、この種の非多様体形状でレイ方向依存の誤判定を起こし得るが、
    フラッドフィルは表面ボクセルシェルの隙間の有無のみに依存するため頑健。"""
    verts_a, indices_a = _duplicated_vertex_box([1.0, 1.0, 1.0], [0.5, 0.5, 0.5])
    verts_b, indices_b = _duplicated_vertex_box([1.0, 1.0, 1.0], [1.5, 0.5, 0.5])
    offset = len(verts_a)
    vertices = verts_a + verts_b
    indices = indices_a + [i + offset for i in indices_b]
    size = 0.25  # 立方体1個あたり4x4x4=64セル(内部8セル)、合計8x4x4=128セル

    _, surface = voxelize_mesh(vertices, indices, size)
    _, filled = voxelize_mesh(vertices, indices, size, fill=True)

    assert len(surface) == (64 - 8) * 2
    assert len(filled) == 128


def test_voxelize_fill_of_box_missing_one_face_leaves_interior_unfilled():
    """真の穴（境界エッジ）を持つ形状（上面が欠損した箱）では、外部フラッド
    フィルがその穴から内部へ「漏れる」ため内部は充填されない。これは頑健性の
    欠如ではなく、そもそも閉じていない形状に対する妥当な結果であり、
    Issue #36 / E7-2 の既知の限界として明示的にテストする。"""
    vertices, indices = _open_box_missing_one_face([1.0, 1.0, 1.0], [0.5, 0.5, 0.5])
    size = 0.25

    _, surface = voxelize_mesh(vertices, indices, size)
    _, filled = voxelize_mesh(vertices, indices, size, fill=True)

    assert filled == surface


# --- 入力検証 ---


def test_voxelize_rejects_non_positive_size(stage):
    vertices, indices = world_mesh(stage, wall_mesh_path(stage, "Wall North"))
    with pytest.raises(ValueError):
        voxelize_mesh(vertices, indices, size=0)
    with pytest.raises(ValueError):
        voxelize_mesh(vertices, indices, size=-0.5)


def test_voxelize_rejects_non_finite_vertices(stage):
    vertices, indices = world_mesh(stage, wall_mesh_path(stage, "Wall North"))
    bad_vertices = list(vertices)
    bad_vertices[0] = (float("nan"), 0.0, 0.0)
    with pytest.raises(ValueError):
        voxelize_mesh(bad_vertices, indices, size=0.5)


def test_voxelize_rejects_origin_above_mesh_bounds(stage):
    """共有originはメッシュ自身の範囲を含んでいなければならない。"""
    vertices, indices = world_mesh(stage, wall_mesh_path(stage, "Wall North"))
    with pytest.raises(ValueError):
        voxelize_mesh(vertices, indices, size=0.5, origin=(10.0, 10.0, 10.0))
