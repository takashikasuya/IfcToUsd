"""占有ボクセルグリッドから narrow-band SDF（符号付き距離場）を生成する
（`docs/viewer/backlog.md` E5-1）。

`voxel.py` の `voxelize_mesh()` が返す占有ボクセル集合（整数グリッド座標のset）を
入力に、表面から一定セル数以内（narrow-band）の各ボクセルについて、最近傍の
表面ボクセル中心までのワールド距離を計算する。表面から離れた領域は計算・
格納しない（メモリを抑えるのが narrow-band の目的）ため、任意の点に対する
`clearance()` クエリは band 内なら辞書引き、band 外なら直接計算にフォールバック
する。

scipy 等の空間索引ライブラリは新規依存として追加せず、ボクセルグリッドの
整数座標であることを利用した素朴な近傍膨張（dilation）+ numpy のブロード
キャストによる総当たりユークリッド距離計算で実装する。実運用規模の
narrow-band（表面付近の薄い殻）であれば候補数は十分小さく、これで足りる。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np

Voxel = tuple[int, int, int]

# 表面ボクセル数百〜数千・band_width一桁を想定した実運用上限。これを超える
# band_widthはdilation対象を指数的に増やし、続く総当たり距離計算
# （O(候補数 × 表面ボクセル数)）のメモリ・CPUコストを暴走させかねない。
_MAX_BAND_WIDTH = 10

_NEIGHBOR_OFFSETS = [
    (dx, dy, dz)
    for dx in (-1, 0, 1)
    for dy in (-1, 0, 1)
    for dz in (-1, 0, 1)
    if not (dx == 0 and dy == 0 and dz == 0)
]


@dataclass(frozen=True)
class NarrowBandSDF:
    """`build_narrow_band_sdf()` の戻り値。`clearance()` はこれを丸ごと受け取り、
    構築時と異なる `size`/`origin` を渡してしまうAPIの取り違えを構造的に防ぐ。
    """

    values: dict[Voxel, float]
    surface_voxels: frozenset[Voxel]
    origin: tuple[float, float, float]
    size: float


def _dilate(voxels: set[Voxel], radius: int) -> set[Voxel]:
    """voxels を26近傍（Chebyshev距離）で radius セル分膨張させた集合を返す。"""
    result = set(voxels)
    frontier = set(voxels)
    for _ in range(radius):
        next_frontier = set()
        for x, y, z in frontier:
            for dx, dy, dz in _NEIGHBOR_OFFSETS:
                neighbor = (x + dx, y + dy, z + dz)
                if neighbor not in result:
                    next_frontier.add(neighbor)
        result.update(next_frontier)
        frontier = next_frontier
    return result


def _voxel_centers(voxels: Sequence[Voxel], origin: np.ndarray, size: float) -> np.ndarray:
    if not voxels:
        return np.zeros((0, 3), dtype=np.float64)
    arr = np.array(voxels, dtype=np.float64)
    return origin + (arr + 0.5) * size


def _nearest_distances(query_points: np.ndarray, reference_points: np.ndarray) -> np.ndarray:
    """query_points の各点から reference_points への最近傍ユークリッド距離を返す
    （総当たり）。"""
    # (Q, 1, 3) - (1, R, 3) -> (Q, R, 3) -> (Q, R)
    diff = query_points[:, None, :] - reference_points[None, :, :]
    dist = np.sqrt(np.sum(diff * diff, axis=2))
    return dist.min(axis=1)


def build_narrow_band_sdf(
    surface_voxels: set[Voxel],
    solid_voxels: set[Voxel],
    origin: Sequence[float],
    size: float,
    band_width: int = 3,
) -> NarrowBandSDF:
    """narrow-band SDF を構築する。

    Args:
        surface_voxels: 表面占有ボクセル（`voxelize_mesh(..., fill=False)`）。
            距離0の面（ゼロ等位集合）となる。
        solid_voxels: 内部充填を含む占有ボクセル（`voxelize_mesh(..., fill=True)`）。
            `surface_voxels` のスーパーセットを想定。ここに含まれるボクセルは
            符号を負にする。fill情報が無い場合は `surface_voxels` をそのまま
            渡せば、内部ボクセルが存在しないため実質的に非負として扱われる。
        origin: `surface_voxels`/`solid_voxels` の整数座標に対応するワールド座標
            原点（`voxelize_mesh()` が返すもの）。
        size: ボクセル一辺の長さ（m）。
        band_width: 表面から何セル分までを narrow-band として計算するか
            （`_MAX_BAND_WIDTH` を超える値は拒否する）。

    Returns:
        `NarrowBandSDF`（band外のボクセルは`values`に含まない）。

    Raises:
        ValueError: `band_width` が負、または `_MAX_BAND_WIDTH` を超える場合。
    """
    if band_width < 0 or band_width > _MAX_BAND_WIDTH:
        raise ValueError(f"band_width must be in [0, {_MAX_BAND_WIDTH}], got {band_width}")

    origin_arr = np.asarray(origin, dtype=np.float64)

    if not surface_voxels:
        return NarrowBandSDF(values={}, surface_voxels=frozenset(), origin=tuple(origin_arr), size=size)

    surface_centers = _voxel_centers(sorted(surface_voxels), origin_arr, size)
    candidates = _dilate(surface_voxels, band_width)

    candidate_list = sorted(candidates)
    candidate_centers = _voxel_centers(candidate_list, origin_arr, size)
    distances = _nearest_distances(candidate_centers, surface_centers)

    values: dict[Voxel, float] = {}
    for voxel, distance in zip(candidate_list, distances):
        sign = -1.0 if voxel in solid_voxels else 1.0
        values[voxel] = sign * float(distance)

    return NarrowBandSDF(
        values=values,
        surface_voxels=frozenset(surface_voxels),
        origin=tuple(origin_arr),
        size=size,
    )


def clearance(point: Sequence[float], sdf: NarrowBandSDF) -> Optional[float]:
    """任意のワールド座標点から最近傍の占有表面までの距離（常に非負）を返す。

    narrow-band内なら `sdf.values` の格納値を、band外なら `sdf.surface_voxels`
    に対する直接計算にフォールバックして求める——narrow-bandはメモリ節約のための
    最適化であり、呼び出し側がband境界を意識せず常に有効な距離を得られるように
    するため。`sdf.values` 自体は内部/外部の符号付き距離（負=内部）だが、
    band外フォールバックは符号を判定する材料（`solid_voxels`）を持たないため
    常に非負の大きさしか返せない。`clearance()` はこの境界をまたいでも呼び出し側
    から見た意味が変わらないよう、band内ヒットも `abs()` を取り常に非負の
    「距離」として統一する（内部/外部を区別したい呼び出し元は、band内である
    ことが分かっているなら `sdf.values` を直接引くこと）。`surface_voxels`が
    空の場合のみNoneを返す（クエリ対象が無い）。
    """
    point_arr = np.asarray(point, dtype=np.float64)
    origin_arr = np.asarray(sdf.origin, dtype=np.float64)
    voxel_index = tuple(int(np.floor(v)) for v in (point_arr - origin_arr) / sdf.size)

    if voxel_index in sdf.values:
        return abs(sdf.values[voxel_index])

    if not sdf.surface_voxels:
        return None

    surface_centers = _voxel_centers(sorted(sdf.surface_voxels), origin_arr, sdf.size)
    distances = _nearest_distances(point_arr[None, :], surface_centers)
    return float(distances[0])
