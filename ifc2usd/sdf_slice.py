"""要素ごとの narrow-band SDF を水平スライスとしてJSON化する
（`docs/viewer/backlog.md` E5-3「Webレイマーチ表示」）。

`sdf.py` の narrow-band SDF は要素単位の疎な辞書であり、そのままではWebビューワーの
表示に使えない。本モジュールは要素ごとに `voxel.py`（表面/内部占有ボクセル）と
`sdf.py`（narrow-band距離場）を組み合わせ、垂直方向を等間隔にスライスした密な2Dグリッド
（符号付き距離値、narrow-band外は`None`）をJSON化する。

真のGPUボリュームレイマーチ（3Dテクスチャ + フラグメントシェーダーでのレイステップ）は
OpenVDB等の新規重量級依存やシェーダー実装コストが大きく、この環境ではOpenVDBの
Python実装がpipで配布されていないため見送る（Issue #28のUsdVol+OpenVDBAsset出力も
同じ理由で保留）。backlog.mdの受け入れ条件「場の等値面/スライスが表示される」の
うちスライス表示のみで満たす。将来ボリュームテクスチャへ拡張する場合も、この密グリッドを
そのままテクスチャソースとして再利用できる。
"""

from __future__ import annotations

import logging
from typing import Sequence

import numpy as np

from .sdf import build_narrow_band_sdf
from .voxel import VoxelElement, voxelize_mesh

logger = logging.getLogger("ifc2usd")

_JSON_VERSION = 1

# 1要素・1スライスあたりのグリッドセル数上限。フロア全体を覆うような巨大要素でも
# JSONサイズが際限なく膨らまないようにする安全弁。超過した要素はスライスを
# 生成せず、その旨をログに残す（出力から静かに欠落させると原因を追えなくなるため）。
#
# これはXY足跡（出力サイズ）だけを抑える安全弁であり、build_narrow_band_sdf内部の
# 総当たり距離計算（O(dilate後候補数 x 表面ボクセル数)）の支配的コストは表面ボクセル数
# そのもの（3次元、Z方向にいくら伸びても足跡には現れない）に由来する。例えば
# 断面2x2セル・高さ1000セルの細長い柱は cols*rows=4 でこの上限を通過するが、
# 表面ボクセル数は約4000にもなり得るため、下の_MAX_SURFACE_VOXELSで別途抑える。
_MAX_GRID_CELLS = 40_000

# 1要素あたりの表面ボクセル数上限（上記の通りcols*rowsでは捉えられないコストの
# 安全弁）。実データ（ToyodaLab.ifc）で観測された実要素は数百〜1000弱程度のため、
# 数倍の余裕を持たせた値にする。
_MAX_SURFACE_VOXELS = 2_000


def build_sdf_slices_json(
    elements: Sequence[VoxelElement],
    size: float,
    slice_count: int = 5,
    band_width: int = 3,
) -> dict:
    """要素ごとのSDF水平スライスをJSON化する。

    Args:
        elements: `elements_from_stage()` が返す要素列。
        size: ボクセル一辺の長さ（m）。voxels.jsonのLODとは独立に指定できる
            （スライス表示は密グリッドを1解像度だけ使えば足り、複数LODぶん
            生成してもコストが線形に増えるだけで表示上の利点がないため）。
        slice_count: 要素ごとに生成する水平スライスの最大枚数。要素の鉛直方向
            ボクセル数がこれより少なければ、実際のスライス数はその数に切り詰める。
        band_width: `build_narrow_band_sdf` に渡すnarrow-band幅。

    Returns:
        ``{"version": 1, "size": size, "elements": {guid: {cols, rows, originX,
        originY, size, slices: [{z, values}]}}}``。``values`` は
        ``rows`` 行 x ``cols`` 列の符号付き距離値（narrow-band外は ``None``）。
        グリッドは表面ボクセルのXYバウンディングボックスに限定するため、
        `band_width` で外側へ広がったnarrow-band自体（表面から離れた外部の
        clearance値）はこのグリッドの外側では切り捨てられる（要素の輪郭より
        外側まで含めた「周囲の場」を見せる用途ではなく、要素自身の断面を見せる
        用途のため）。頂点が無い要素、表面ボクセルが0個の要素、表面ボクセル数が
        `_MAX_SURFACE_VOXELS` を超える要素、グリッドセル数が `_MAX_GRID_CELLS`
        を超える要素はスキップする。
    """
    result_elements: dict[str, dict] = {}

    for el in elements:
        if not len(el.vertices):
            continue

        origin, surface_voxels = voxelize_mesh(el.vertices, el.indices, size=size, fill=False)
        if not surface_voxels:
            continue
        if len(surface_voxels) > _MAX_SURFACE_VOXELS:
            logger.warning(
                "build_sdf_slices_json: skipping element %s (%d surface voxels exceeds %d-voxel cap)",
                el.guid, len(surface_voxels), _MAX_SURFACE_VOXELS,
            )
            continue
        _, solid_voxels = voxelize_mesh(el.vertices, el.indices, size=size, origin=origin, fill=True)

        ix_min = min(v[0] for v in surface_voxels)
        ix_max = max(v[0] for v in surface_voxels)
        iy_min = min(v[1] for v in surface_voxels)
        iy_max = max(v[1] for v in surface_voxels)
        iz_min = min(v[2] for v in surface_voxels)
        iz_max = max(v[2] for v in surface_voxels)

        cols = ix_max - ix_min + 1
        rows = iy_max - iy_min + 1
        if cols * rows > _MAX_GRID_CELLS:
            logger.warning(
                "build_sdf_slices_json: skipping element %s (%d x %d grid exceeds %d-cell cap)",
                el.guid, cols, rows, _MAX_GRID_CELLS,
            )
            continue

        sdf = build_narrow_band_sdf(surface_voxels, solid_voxels, origin, size, band_width)

        iz_range = iz_max - iz_min + 1
        n_slices = min(slice_count, iz_range)
        iz_choices = sorted(set(int(round(v)) for v in np.linspace(iz_min, iz_max, n_slices)))

        slices = []
        for iz in iz_choices:
            world_z = origin[2] + (iz + 0.5) * size
            values = [
                [sdf.values.get((ix, iy, iz)) for ix in range(ix_min, ix_max + 1)]
                for iy in range(iy_min, iy_max + 1)
            ]
            slices.append({"z": world_z, "values": values})

        result_elements[el.guid] = {
            "cols": cols,
            "rows": rows,
            "originX": origin[0] + ix_min * size,
            "originY": origin[1] + iy_min * size,
            "size": size,
            "slices": slices,
        }

    return {"version": _JSON_VERSION, "size": size, "elements": result_elements}
