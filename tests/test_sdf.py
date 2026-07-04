"""sdf.py（占有ボクセル → narrow-band SDF）のテスト（Issue #27 / E5-1）。"""

from __future__ import annotations

import pytest

from ifc2usd.sdf import build_narrow_band_sdf, clearance


def _cube_shell(lo, hi):
    """[lo, hi]の閉区間で構成される立方体シェル（表面ボクセルのみ）の集合を返す。"""
    shell = set()
    for x in range(lo, hi + 1):
        for y in range(lo, hi + 1):
            for z in range(lo, hi + 1):
                on_boundary = x in (lo, hi) or y in (lo, hi) or z in (lo, hi)
                if on_boundary:
                    shell.add((x, y, z))
    return shell


def _cube_solid(lo, hi):
    """[lo, hi]の閉区間で構成される立方体（表面+内部）の集合を返す。"""
    return {
        (x, y, z)
        for x in range(lo, hi + 1)
        for y in range(lo, hi + 1)
        for z in range(lo, hi + 1)
    }


ORIGIN0 = (0.0, 0.0, 0.0)


def test_empty_surface_returns_empty_sdf():
    sdf = build_narrow_band_sdf(set(), set(), ORIGIN0, size=1.0, band_width=3)
    assert sdf.values == {}


def test_surface_voxels_have_zero_distance():
    surface = _cube_shell(0, 4)
    solid = _cube_solid(0, 4)
    sdf = build_narrow_band_sdf(surface, solid, ORIGIN0, size=1.0, band_width=2)

    for v in surface:
        assert sdf.values[v] == pytest.approx(0.0, abs=1e-9)


def test_interior_voxels_have_negative_distance():
    surface = _cube_shell(0, 6)
    solid = _cube_solid(0, 6)
    sdf = build_narrow_band_sdf(surface, solid, ORIGIN0, size=1.0, band_width=2)

    # (3,3,3)は6x6x6立方体のほぼ中心で表面から離れているため、band_width=2の
    # narrow-bandには含まれない。band内の内部ボクセル、例えば表面のすぐ内側
    # (1,1,1)（表面(0,*,*)等から距離1）は負の値を持つはず。
    assert (1, 1, 1) not in surface
    assert (1, 1, 1) in solid
    assert (1, 1, 1) in sdf.values
    assert sdf.values[(1, 1, 1)] < 0


def test_exterior_voxels_have_positive_distance():
    surface = _cube_shell(0, 4)
    solid = _cube_solid(0, 4)
    sdf = build_narrow_band_sdf(surface, solid, ORIGIN0, size=1.0, band_width=2)

    # (-1, 2, 2)は表面のすぐ外側（x=0の面から距離1）。
    outside_voxel = (-1, 2, 2)
    assert outside_voxel not in solid
    assert outside_voxel in sdf.values
    assert sdf.values[outside_voxel] > 0


def test_narrow_band_excludes_voxels_far_from_surface():
    surface = _cube_shell(0, 10)
    solid = _cube_solid(0, 10)
    sdf = build_narrow_band_sdf(surface, solid, ORIGIN0, size=1.0, band_width=2)

    # (5,5,5)は11x11x11立方体(0..10)の中心付近で、どの表面ボクセルからも
    # band_width=2セルより離れている。narrow-bandの定義上、含まれないはず。
    assert (5, 5, 5) not in sdf.values


def test_distance_scales_with_voxel_size():
    surface = _cube_shell(0, 4)
    solid = _cube_solid(0, 4)
    sdf_1m = build_narrow_band_sdf(surface, solid, ORIGIN0, size=1.0, band_width=2)
    sdf_2m = build_narrow_band_sdf(surface, solid, ORIGIN0, size=2.0, band_width=2)

    outside_voxel = (-1, 2, 2)
    assert sdf_2m.values[outside_voxel] == pytest.approx(sdf_1m.values[outside_voxel] * 2.0)


def test_rejects_out_of_range_band_width():
    surface = _cube_shell(0, 2)
    solid = _cube_solid(0, 2)
    with pytest.raises(ValueError):
        build_narrow_band_sdf(surface, solid, ORIGIN0, size=1.0, band_width=-1)
    with pytest.raises(ValueError):
        build_narrow_band_sdf(surface, solid, ORIGIN0, size=1.0, band_width=1_000_000)


def test_clearance_query_matches_known_height_above_flat_surface():
    """XY平面(z=0)に広がる表面ボクセルからの高さクエリが正しい値を返す。"""
    size = 0.5
    surface = {(x, y, 0) for x in range(20) for y in range(20)}
    solid = surface  # 平面自体には内部が無い

    sdf = build_narrow_band_sdf(surface, solid, ORIGIN0, size=size, band_width=4)

    # 表面ボクセルの中心の真上、2セル分（=1.0m）の高さの点でクエリする。
    point = (2.25, 2.25, 1.0)
    result = clearance(point, sdf)
    assert result == pytest.approx(1.0, abs=0.05)


def test_clearance_falls_back_outside_narrow_band():
    """narrow-bandの範囲外の遠い点でも、フォールバックにより正しい距離を返す
    （Noneにならない・クラッシュしない）。"""
    size = 1.0
    surface = _cube_shell(0, 4)
    solid = _cube_solid(0, 4)
    sdf = build_narrow_band_sdf(surface, solid, ORIGIN0, size=size, band_width=1)

    far_point = (100.0, 2.5, 2.5)
    result = clearance(far_point, sdf)
    assert result is not None
    # (4,*,*)面（x=4のセル、中心x=4.5）から100mの点までの距離
    assert result == pytest.approx(100.0 - 4.5, abs=0.5)


def test_clearance_returns_zero_at_surface():
    size = 1.0
    surface = _cube_shell(0, 4)
    solid = _cube_solid(0, 4)
    sdf = build_narrow_band_sdf(surface, solid, ORIGIN0, size=size, band_width=2)

    # 表面ボクセル(0,2,2)の中心そのもの。
    point = (0.5, 2.5, 2.5)
    result = clearance(point, sdf)
    assert result == pytest.approx(0.0, abs=1e-6)


def test_clearance_fallback_respects_nonzero_origin():
    """回帰テスト: clearance()のband外フォールバックがoriginを無視すると、
    origin!=(0,0,0)のシーンで距離が原点オフセット分ずれて返ってしまっていた
    （band内のヒットはoriginを正しく使っており気付きにくいバグだった）。"""
    size = 1.0
    origin = (100.0, 200.0, 300.0)
    surface = _cube_shell(0, 4)
    solid = _cube_solid(0, 4)
    sdf = build_narrow_band_sdf(surface, solid, origin, size=size, band_width=1)

    # 表面のワールド座標での外接範囲は origin+[0,5]。x方向にoriginから110、
    # つまり面(x=4, world center=104.5)から5.5mの、band外の点。
    far_point = (origin[0] + 10.0, origin[1] + 2.5, origin[2] + 2.5)
    result = clearance(far_point, sdf)
    assert result == pytest.approx(10.0 - 4.5, abs=1e-6)


def test_clearance_is_always_non_negative_even_for_interior_points():
    """回帰テスト（PRレビューで発見）: clearance()の意味は「距離」であるべきだが、
    band内ヒットはsdf.valuesの符号付き値（内部=負）をそのまま返しており、
    band外フォールバック（常に非負の大きさのみ）と符号の扱いが不整合だった
    （呼び出し側から見て、band境界をまたぐとclearance()の意味が変わってしまう）。
    band内・band外のどちらでも常に非負の距離を返すことを確認する。"""
    size = 1.0
    surface = _cube_shell(0, 4)
    solid = _cube_solid(0, 4)
    sdf = build_narrow_band_sdf(surface, solid, ORIGIN0, size=size, band_width=2)

    # 表面のすぐ内側、band内の内部点。sdf.values自体は負値のはず。
    interior_voxel = (1, 2, 2)
    assert sdf.values[interior_voxel] < 0

    interior_point = (1.5, 2.5, 2.5)
    result = clearance(interior_point, sdf)
    assert result is not None
    assert result >= 0
    assert result == pytest.approx(abs(sdf.values[interior_voxel]))
