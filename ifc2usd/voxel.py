"""メッシュの占有ボクセル化と Morton (Z-order) 符号化。

`docs/viewer/spec.md` §1.1, §2 に対応する。ボクセル化はメッシュの表面（既定）
または内部充填（``fill=True``）を占有格子（``origin`` からの整数インデックス
``(ix, iy, iz)``）として返す。占有判定は自前実装（各三角形のAABBを占有格子へ
展開する方式）で行い、内部充填のみ trimesh の point-in-mesh 判定を利用する。
Morton符号化はビット桁のインタリーブによる自前実装。
"""

from __future__ import annotations

import math
from typing import Sequence

import numpy as np
import trimesh


def morton_encode(x: int, y: int, z: int) -> int:
    """3軸の非負整数座標から Morton (Z-order) 符号を計算する。"""
    if x < 0 or y < 0 or z < 0:
        raise ValueError(f"morton_encode requires non-negative coordinates, got ({x}, {y}, {z})")

    bits = max(x.bit_length(), y.bit_length(), z.bit_length(), 1)
    code = 0
    for i in range(bits):
        code |= ((x >> i) & 1) << (3 * i)
        code |= ((y >> i) & 1) << (3 * i + 1)
        code |= ((z >> i) & 1) << (3 * i + 2)
    return code


def morton_decode(code: int) -> tuple[int, int, int]:
    """Morton (Z-order) 符号から3軸の整数座標を復元する。"""
    if code < 0:
        raise ValueError(f"morton_decode requires a non-negative code, got {code}")

    x = y = z = 0
    i = 0
    while code >> (3 * i):
        x |= ((code >> (3 * i)) & 1) << i
        y |= ((code >> (3 * i + 1)) & 1) << i
        z |= ((code >> (3 * i + 2)) & 1) << i
        i += 1
    return x, y, z


def _mesh_bounds(vertices: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    return vertices.min(axis=0), vertices.max(axis=0)


def _snap_to_integer(value: np.ndarray, tol: float = 1e-9) -> np.ndarray:
    """浮動小数点誤差で格子線からわずかにずれた値を、最も近い整数へ吸着させる。"""
    rounded = np.round(value)
    return np.where(np.abs(value - rounded) < tol, rounded, value)


def _surface_voxels(
    vertices: np.ndarray, triangles: np.ndarray, size: float, origin: np.ndarray
) -> set[tuple[int, int, int]]:
    """各三角形のAABBが格子セルの開区間と重なるものを占有とみなす。

    セル i は開区間 (i*size, (i+1)*size) を占有領域とする。三角形のAABBが
    ちょうど格子線上に乗る退化面（厚み0）は、その軸で空区間（lo>hi）となり
    寄与しない ── 閉多面体では隣接する非退化面が同じ境界セルを別途カバーする
    ため、表面占有としては欠落しない。
    """
    voxels: set[tuple[int, int, int]] = set()
    for tri in triangles:
        tri_verts = vertices[tri]
        tri_min = tri_verts.min(axis=0)
        tri_max = tri_verts.max(axis=0)
        lo_raw = _snap_to_integer((tri_min - origin) / size)
        hi_raw = _snap_to_integer((tri_max - origin) / size)
        lo = np.maximum(np.floor(lo_raw), 0).astype(int)
        hi = (np.ceil(hi_raw) - 1).astype(int)
        if np.any(hi < lo):
            continue
        for ix in range(lo[0], hi[0] + 1):
            for iy in range(lo[1], hi[1] + 1):
                for iz in range(lo[2], hi[2] + 1):
                    voxels.add((ix, iy, iz))
    return voxels


def _fill_voxels(
    mesh: trimesh.Trimesh, surface: set[tuple[int, int, int]], size: float, origin: np.ndarray
) -> set[tuple[int, int, int]]:
    """surface のAABB範囲内で、中心点がメッシュ内部にあるセルを追加する。"""
    if not surface:
        return set(surface)

    xs = [v[0] for v in surface]
    ys = [v[1] for v in surface]
    zs = [v[2] for v in surface]
    lo = (min(xs), min(ys), min(zs))
    hi = (max(xs), max(ys), max(zs))

    candidates = [
        (ix, iy, iz)
        for ix in range(lo[0], hi[0] + 1)
        for iy in range(lo[1], hi[1] + 1)
        for iz in range(lo[2], hi[2] + 1)
        if (ix, iy, iz) not in surface
    ]
    if not candidates:
        return set(surface)

    centers = origin + (np.array(candidates) + 0.5) * size
    inside = mesh.contains(centers)
    filled = set(surface)
    filled.update(idx for idx, is_inside in zip(candidates, inside) if is_inside)
    return filled


def voxelize_mesh(
    vertices: Sequence[Sequence[float]],
    indices: Sequence[int],
    size: float,
    origin: Sequence[float] | None = None,
    fill: bool = False,
) -> tuple[tuple[float, float, float], set[tuple[int, int, int]]]:
    """三角形メッシュを占有ボクセル化する。

    Args:
        vertices: ワールド座標の頂点列。
        indices: 三角形の頂点インデックス（flat, 3個ずつ組）。
        size: ボクセル一辺の長さ（m）。
        origin: ボクセル格子原点（ワールド座標）。省略時はメッシュのAABB最小点。
        fill: True の場合、表面占有に加えて内部（trimeshのcontains判定）も占有とする。

    Returns:
        (使用した origin, 占有格子インデックス (ix, iy, iz) の集合)
    """
    verts = np.asarray(vertices, dtype=np.float64)
    triangles = np.asarray(indices, dtype=np.int64).reshape(-1, 3)

    if origin is None:
        origin_arr, _ = _mesh_bounds(verts)
    else:
        origin_arr = np.asarray(origin, dtype=np.float64)

    surface = _surface_voxels(verts, triangles, size, origin_arr)

    if fill:
        mesh = trimesh.Trimesh(vertices=verts, faces=triangles, process=False)
        occupied = _fill_voxels(mesh, surface, size, origin_arr)
    else:
        occupied = surface

    return tuple(origin_arr.tolist()), occupied
