"""メッシュの占有ボクセル化と Morton (Z-order) 符号化。

`docs/viewer/spec.md` §1.1, §2 に対応する。ボクセル化はメッシュの表面（既定）
または内部充填（``fill=True``）を占有格子（``origin`` からの整数インデックス
``(ix, iy, iz)``）として返す。占有判定は自前実装（各三角形のAABBを占有格子へ
展開する方式）で行い、内部充填のみ trimesh の point-in-mesh 判定を利用する。
Morton符号化はビット桁のインタリーブによる自前実装。
"""

from __future__ import annotations

import logging
import math
from typing import NamedTuple, Optional, Sequence

import numpy as np
import trimesh
from pxr import Gf, Sdf, Usd, UsdGeom

logger = logging.getLogger("ifc2usd")

_JSON_VERSION = 2
_UNITS = "m"


class VoxelElement(NamedTuple):
    """ボクセル化対象の1要素（IFCエレメント相当）。"""

    guid: str
    cls: str
    name: Optional[str]
    color: tuple[float, float, float]
    vertices: Sequence[Sequence[float]]
    indices: Sequence[int]


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


def _snap_to_integer(value: np.ndarray, tol: float = 1e-9) -> np.ndarray:
    """浮動小数点誤差で格子線からわずかにずれた値を、最も近い整数へ吸着させる。"""
    rounded = np.round(value)
    return np.where(np.abs(value - rounded) < tol, rounded, value)


def _surface_voxels(
    vertices: np.ndarray, triangles: np.ndarray, size: float, origin: np.ndarray
) -> set[tuple[int, int, int]]:
    """各三角形のAABBが格子セルの開区間と重なるものを占有とみなす。

    セル i は開区間 (i*size, (i+1)*size) を占有領域とする。三角形のAABBが
    ちょうど格子線上に乗る退化面（厚み0、例: 軸並行ボックスの各面）は、
    素朴な開区間判定では空区間（lo>hi）になり得る ── その値がメッシュ全体の
    下端/上端と一致するなら「材質はその側にしかない」ことが確定するので
    一意に解決できる（下端なら格子線の上のセル、上端なら下のセルに属する）。
    どちらとも一致しない内部の退化面（稀）は、安全側として両隣接セルに含める。
    このメッシュ全体の下端/上端判定がないと、寸法が格子サイズの整数倍に
    ちょうど揃った形状（例: 1m立方体を0.5m格子で処理）で全面が退化面かつ
    格子線上に乗り、表面が丸ごと消失するバグになる。

    既知の制限: 判定はAABB単位であり、真の三角形-ボックス交差判定ではない。
    軸並行な形状（壁・床など典型的なBIM要素）ではAABBが実際の面と一致するため
    厳密だが、斜めの三角形（傾斜屋根・筋交いなど）ではAABBが実面より広くなり、
    占有ボクセルを過大評価する（false positive）。

    呼び出し側（voxelize_mesh）で `origin` がメッシュのAABB最小点以下である
    ことを検証済みのため、ここでは格子インデックスが負にならない前提で計算する。
    """
    mesh_min = vertices.min(axis=0)
    mesh_max = vertices.max(axis=0)
    tol = 1e-9

    voxels: set[tuple[int, int, int]] = set()
    for tri in triangles:
        tri_verts = vertices[tri]
        tri_min = tri_verts.min(axis=0)
        tri_max = tri_verts.max(axis=0)
        lo_raw = _snap_to_integer((tri_min - origin) / size)
        hi_raw = _snap_to_integer((tri_max - origin) / size)
        lo = np.floor(lo_raw).astype(int)
        hi = (np.ceil(hi_raw) - 1).astype(int)

        for axis in range(3):
            if hi[axis] >= lo[axis]:
                continue
            k = lo[axis]  # 格子線の整数位置（degenerate点 = k*size）
            value = tri_min[axis]
            if math.isclose(value, mesh_min[axis], abs_tol=tol):
                lo[axis] = hi[axis] = k
            elif math.isclose(value, mesh_max[axis], abs_tol=tol):
                lo[axis] = hi[axis] = k - 1
            else:
                lo[axis] = max(k - 1, 0)
                hi[axis] = k

        lo = np.maximum(lo, 0)
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

    Raises:
        ValueError: `size` が正でない、頂点に非有限値（NaN/Inf）が含まれる、
            または指定 `origin` がメッシュのAABB最小点を上回っている場合
            （シーン共有originはメッシュ自身の範囲を含んでいなければならない）。
    """
    if size <= 0:
        raise ValueError(f"size must be positive, got {size}")

    verts = np.asarray(vertices, dtype=np.float64)
    if not np.all(np.isfinite(verts)):
        raise ValueError("vertices must be finite (no NaN/Inf)")
    triangles = np.asarray(indices, dtype=np.int64).reshape(-1, 3)

    mesh_min = verts.min(axis=0)
    if origin is None:
        origin_arr = mesh_min
    else:
        origin_arr = np.asarray(origin, dtype=np.float64)
        if np.any(mesh_min < origin_arr - 1e-9):
            raise ValueError(
                f"origin {tuple(origin_arr)} must be <= the mesh's own bounds min {tuple(mesh_min)}"
            )

    surface = _surface_voxels(verts, triangles, size, origin_arr)

    if fill:
        # process=True（既定）で頂点溶接・退化面除去を行う。ifc.py は
        # weld-vertices=False で法線を保持するため、各面が独自の頂点を持ち
        # 未処理では非水密メッシュとなり mesh.contains() が信頼できない。
        mesh = trimesh.Trimesh(vertices=verts, faces=triangles)
        if not mesh.is_watertight:
            logger.warning(
                "voxelize_mesh: mesh is not watertight after processing; "
                "fill=True interior detection may be unreliable"
            )
        occupied = _fill_voxels(mesh, surface, size, origin_arr)
    else:
        occupied = surface

    return tuple(origin_arr.tolist()), occupied


def scene_origin(elements: Sequence[VoxelElement]) -> tuple[float, float, float]:
    """複数要素のワールド座標頂点から、共有originとなるシーン全体のAABB最小点を求める。"""
    mins = []
    for el in elements:
        if not len(el.vertices):
            continue
        verts = np.asarray(el.vertices, dtype=np.float64)
        if not np.all(np.isfinite(verts)):
            # NaN/Inf は np.min に伝播し、他の正常な要素の origin まで汚染するため
            # ここで検出する（voxelize_mesh 自身の検証は要素ごとで、原点計算より後）。
            raise ValueError(f"element {el.guid!r} has non-finite vertices")
        mins.append(verts.min(axis=0))
    if not mins:
        raise ValueError("scene_origin requires at least one element with vertices")
    return tuple(np.min(mins, axis=0).tolist())


def build_voxel_json(
    elements: Sequence[VoxelElement],
    sizes: Sequence[float],
    source: Optional[dict] = None,
    up_axis: str = "Z",
    fill: bool = False,
) -> dict:
    """`docs/viewer/spec.md` §2 のボクセル JSON v2 を構築する。

    全要素・全LODで共有する単一の `origin`（シーン全体のワールドAABB最小点）を
    用いるため、`origin + index*size` はどの要素・どのLODでも同じワールド座標系
    に一致する。頂点を持たない要素は出力から除外する。ボクセル化した結果
    占有ボクセルが0個になった要素は、`indices: []` として出力する（他のLODには
    出現するのにこのLODでは要素自体が消えてしまうと、ビューワー側で「このLODに
    存在しない」のか「存在するが空」なのか区別できなくなるため）。
    """
    origin = scene_origin(elements)

    lods = []
    for size in sizes:
        lod_elements = []
        for el in elements:
            if not len(el.vertices):
                continue
            _, voxels = voxelize_mesh(el.vertices, el.indices, size, origin=origin, fill=fill)
            indices = sorted(morton_encode(*v) for v in voxels)
            lod_elements.append(
                {
                    "guid": el.guid,
                    "class": el.cls,
                    "name": el.name,
                    "color": list(el.color),
                    "indices": indices,
                }
            )
        lods.append({"size": size, "elements": lod_elements})

    return {
        "version": _JSON_VERSION,
        "units": _UNITS,
        "upAxis": up_axis,
        "source": source or {},
        "origin": list(origin),
        "lods": lods,
    }


def _variant_name(size: float) -> str:
    return f"size_{size}".replace(".", "_")


def build_voxel_stage(
    elements: Sequence[VoxelElement],
    sizes: Sequence[float],
    reference_asset_path: str,
    output_path: str,
    reference_prim_path: str = "/IFC_Model",
    up_axis: str = "Z",
    fill: bool = False,
) -> str:
    """`docs/viewer/spec.md` §3 の PointInstancer ボクセルレイヤーを構築し、
    `output_path` へ書き出す。

    正本 USD（`reference_asset_path`）への reference のみを持つ独立したレイヤーで、
    正本自体は書き換えない。`reference_asset_path` は `output_path` から見た
    相対パスにすること。

    書き出しは `stage.GetRootLayer().Export(...)` を用いる（内部の実装詳細だが
    重要な注意点）。`Usd.Stage.Export()` はステージを「現在選択中のvariantで
    合成された1枚のフラットな結果」として書き出すため、variantSet自体（他の
    variantの内容や `variantSets`/`variants` の合成情報）が失われる。variantを
    ビューワー側で切り替え可能な形で保持するには、raw な root layer をそのまま
    書き出す必要がある。この関数がその区別を吸収するため、呼び出し側は本関数の
    返り値（書き出し先パス）だけを見ればよい。

    要素（GUID）ごとに1 prototype（サイズ・displayColor付きCube）を割り当て、
    `customData["elementRanges"]` に GUID -> [start, count]（positions/
    protoIndices 内でのインスタンス範囲）を記録し、ビューワーからの逆引きを
    可能にする。LODサイズごとに `voxelLOD` variantSet の1 variantを対応させ、
    既定 variant は `sizes` の先頭とする。
    """
    if not sizes:
        raise ValueError("sizes must be non-empty")
    # 重複サイズは同じvariant名を再定義し、ボクセル化を無駄に繰り返すだけなので
    # 順序を保ったまま除去する。
    unique_sizes = list(dict.fromkeys(sizes))

    origin = scene_origin(elements)

    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y if up_axis == "Y" else UsdGeom.Tokens.z)
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)

    root_path = Sdf.Path(reference_prim_path)
    root = UsdGeom.Xform.Define(stage, root_path)
    root.GetPrim().GetReferences().AddReference(reference_asset_path, root_path)
    stage.SetDefaultPrim(root.GetPrim())

    voxels_path = root_path.AppendChild("Voxels")
    instancer = UsdGeom.PointInstancer.Define(stage, voxels_path)
    UsdGeom.Imageable(instancer.GetPrim()).CreatePurposeAttr().Set(UsdGeom.Tokens.proxy)

    prototypes_path = voxels_path.AppendChild("Prototypes")
    UsdGeom.Scope.Define(stage, prototypes_path)

    variant_set = instancer.GetPrim().GetVariantSets().AddVariantSet("voxelLOD")

    for size in unique_sizes:
        variant_name = _variant_name(size)
        variant_set.AddVariant(variant_name)
        variant_set.SetVariantSelection(variant_name)
        with variant_set.GetVariantEditContext():
            positions: list = []
            proto_indices: list = []
            proto_targets: list = []
            ranges: dict = {}

            for el in elements:
                if not len(el.vertices):
                    continue
                _, voxels = voxelize_mesh(el.vertices, el.indices, size, origin=origin, fill=fill)
                voxel_list = sorted(voxels)

                start = len(positions)
                ranges[el.guid] = [start, len(voxel_list)]
                if not voxel_list:
                    # このLODでは占有ボクセル0個。prototype/instanceは作らず
                    # rangeのみ記録する（JSON v2ライターと同じ「静かに消さない」方針）。
                    continue

                proto_index = len(proto_targets)
                for ix, iy, iz in voxel_list:
                    positions.append(
                        Gf.Vec3f(
                            origin[0] + (ix + 0.5) * size,
                            origin[1] + (iy + 0.5) * size,
                            origin[2] + (iz + 0.5) * size,
                        )
                    )
                    proto_indices.append(proto_index)

                cube_path = prototypes_path.AppendChild(f"{variant_name}_Element_{proto_index}")
                cube = UsdGeom.Cube.Define(stage, cube_path)
                cube.CreateSizeAttr().Set(size)
                cube.GetDisplayColorAttr().Set([Gf.Vec3f(*el.color)])
                proto_targets.append(cube_path)

            instancer.CreatePositionsAttr().Set(positions)
            instancer.CreateProtoIndicesAttr().Set(proto_indices)
            instancer.CreatePrototypesRel().SetTargets(proto_targets)
            instancer.GetPrim().SetCustomDataByKey("elementRanges", ranges)

    variant_set.SetVariantSelection(_variant_name(unique_sizes[0]))

    stage.GetRootLayer().Export(str(output_path))
    return str(output_path)
