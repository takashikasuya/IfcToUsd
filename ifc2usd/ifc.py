"""IFC の読み取り: ジオメトリ抽出とプロパティ抽出。

もとは ``IFC_to_USD.ipynb`` のセルに分散していたロジックを、実行順に依存しない
純粋な関数群として再構成したもの。ifcopenshell 0.8 系の API に対応する。
"""

from __future__ import annotations

import logging
import multiprocessing
from typing import Iterator, NamedTuple, Optional

import ifcopenshell
import ifcopenshell.util.unit
import numpy as np
from ifcopenshell import geom
from pxr import Sdf

logger = logging.getLogger(__name__)

# ジオメトリから除外する空間系エレメント（開口部・空間・ゾーン）
_EXCLUDED_TYPES = ("IfcOpeningElement", "IfcSpace", "IfcSpatialZone")

# IfcElementQuantity の実体型と、値を保持する属性名
_QUANTITY_VALUE_ATTRS = {
    "IfcQuantityArea": "AreaValue",
    "IfcQuantityCount": "CountValue",
    "IfcQuantityLength": "LengthValue",
    "IfcQuantityTime": "TimeValue",
    "IfcQuantityVolume": "VolumeValue",
    "IfcQuantityWeight": "WeightValue",
}

# 読めなかったプロパティの (型, 事由)。同じ組み合わせの警告は1度きりにする
_WARNED_PROPERTY_FAILURES: set[tuple[str, str]] = set()

# 文字ごとの識別子判定は USD 側の規則に委ねる。文字種は限られるのでキャッシュする
_IDENTIFIER_CHAR_CACHE: dict[tuple[str, bool], bool] = {}


def _is_valid_identifier_char(ch: str, *, first: bool) -> bool:
    key = (ch, first)
    cached = _IDENTIFIER_CHAR_CACHE.get(key)
    if cached is None:
        cached = bool(Sdf.Path.IsValidIdentifier(ch if first else f"_{ch}"))
        _IDENTIFIER_CHAR_CACHE[key] = cached
    return cached


def sanitize_material_name(name: str) -> str:
    """マテリアル名を USD の prim 名として有効な識別子へ整形する。

    usd-core 26.5 の prim 名は UTF-8 を受け付けるため、日本語名は保持する
    （``Tf.MakeValidIdentifier`` は ASCII 以外を ``_`` の羅列へ潰してしまう）。
    """
    out = [ch if _is_valid_identifier_char(ch, first=False) else "_" for ch in name]
    if not out:
        return "_"
    if not _is_valid_identifier_char(out[0], first=True):
        out.insert(0, "_")
    return "".join(out)


class MaterialNameRegistry:
    """IFC のマテリアル名へ USD prim 名を一意に割り当てる。

    サニタイズ後は別名同士が衝突しうる（"Concrete 1" と "Concrete-1" など）ため、
    元の名前との 1 対 1 対応をここで保つ。
    """

    def __init__(self) -> None:
        self._assigned: dict[str, str] = {}
        self._used: set[str] = set()

    def resolve(self, name: str) -> str:
        existing = self._assigned.get(name)
        if existing is not None:
            return existing

        base = sanitize_material_name(name)
        candidate = base
        suffix = 2
        while candidate in self._used:
            candidate = f"{base}_{suffix}"
            suffix += 1

        self._assigned[name] = candidate
        self._used.add(candidate)
        return candidate


def zup_to_yup(points: np.ndarray) -> np.ndarray:
    """Z-UP の座標列を Y-UP へ持ち上げる（X 軸まわり −90°の回転）。

    Y/Z の単純な入れ替えは行列式 −1 の鏡像変換になり形状が左右反転するため使わない。
    """
    points = np.asarray(points, dtype=float)
    return np.column_stack((points[:, 0], points[:, 2], -points[:, 1]))


def _color_to_tuple(colour) -> tuple[float, float, float]:
    """ifcopenshell 0.8 の colour オブジェクトを (r, g, b) タプルへ変換する。"""
    return (colour.r(), colour.g(), colour.b())


def _matrix12(matrix) -> list[float]:
    """0.8 の 4x4 列優先フラット行列(16要素)を [X, Y, Z, T] の12要素へ変換する。

    列優先レイアウト: [Xx,Xy,Xz,0, Yx,Yy,Yz,0, Zx,Zy,Zz,0, Tx,Ty,Tz,1]
    """
    return [
        matrix[0], matrix[1], matrix[2],
        matrix[4], matrix[5], matrix[6],
        matrix[8], matrix[9], matrix[10],
        matrix[12], matrix[13], matrix[14],
    ]


def decompose_transform(matrix12) -> tuple[np.ndarray, np.ndarray]:
    """[X, Y, Z, T] の12要素行列を (回転行列, 平行移動) へ分解する。

    先頭3行は X/Y/Z 基底ベクトルなので、列に並べ直して回転行列とする。
    """
    rows = [tuple(matrix12[i:i + 3]) for i in range(0, 12, 3)]
    return np.asarray(rows[:3]).T, np.asarray(rows[3])


class GeometryData(NamedTuple):
    """1 エレメント分のメッシュ。``transform`` はローカル→ワールドの12要素行列。"""

    faces: list
    vertices: list
    indices: list
    material_name: Optional[str]
    color: tuple
    normals: list
    transform: list


class MaterialSpec(NamedTuple):
    """UsdPreviewSurface に必要なマテリアル属性。"""

    diffuse: tuple
    transparency: Optional[float]


def create_settings() -> geom.settings:
    """ifcopenshell 0.8 のジオメトリ設定を生成する。

    0.8 系では設定キーが enum から文字列へ変更されている。
    参考: https://docs.ifcopenshell.org/ifcopenshell/geometry_settings.html
    """
    settings = geom.settings()
    # これがないと normals が破棄される
    settings.set("weld-vertices", False)
    settings.set("apply-default-materials", True)
    return settings


def format_ifc_info(info: dict) -> dict:
    """IfcOpenShell の info は entity_instance を含むので、素の値のみへ変換する。"""
    ret: dict = {}
    for key in info.keys():
        item = info.get(key)
        if not isinstance(item, ifcopenshell.entity_instance) and item is not None:
            ret[key] = item
    return ret


def get_length_unit_scale(ifc_file) -> float:
    """IFC の長さ単位 1 に対するメートル数を返す（mm 記述のモデルなら 0.001）。

    IfcOpenShell のジオメトリ出力自体はこの値で正規化済み（常にメートル）なので、
    ステージの ``metersPerUnit`` ではなく元データの記述単位の記録に用いる。
    """
    return float(ifcopenshell.util.unit.calculate_unit_scale(ifc_file))


def get_project_info(ifc_file, name: str = "Sample") -> tuple[str, str, str]:
    """IfcProject / IfcSite からプロジェクト名と緯度経度を取得する。"""
    prj = ifc_file.by_type("IfcProject")[0]
    name_ = prj.LongName if prj.LongName != "プロジェクト名" else name
    name_ = name if name_ is None else name_

    site = ifc_file.by_type("IfcSite")[0]
    lat = ".".join(str(i) for i in site.RefLatitude) if site.RefLatitude is not None else ""
    lon = ".".join(str(i) for i in site.RefLongitude) if site.RefLongitude is not None else ""
    return name_, lat, lon


def get_geometry(settings, ifc_file, materials: dict) -> Iterator[tuple]:
    """対象オブジェクトのジオメトリを1件ずつ生成する（ジェネレータ）。

    座標は常に IFC 既定の Z-UP。Y-UP への変換は USD ルートの Xform が担う。

    Args:
        settings: ifcopenshell のジオメトリ設定
        ifc_file: 対象の IFC ファイル
        materials: マテリアル名→MaterialSpec を蓄積する辞書
    """
    iterator = geom.iterator(settings, ifc_file, multiprocessing.cpu_count())

    if not iterator.initialize():
        return

    material_names = MaterialNameRegistry()

    while True:
        shape = iterator.get()
        element = ifc_file.by_guid(shape.guid)

        # 空間系エレメントはジオメトリから除く
        if any(element.is_a(t) for t in _EXCLUDED_TYPES):
            if not iterator.next():
                break
            continue

        info = format_ifc_info(element.get_info())

        matrix = _matrix12(shape.transformation.matrix)
        verts = shape.geometry.verts
        indices = shape.geometry.faces
        # 頂点法線。IfcOpenShell の出力は反転しているため符号を戻す
        norms = [n * -1 for n in shape.geometry.normals]

        grouped_verts = [(verts[i], verts[i + 1], verts[i + 2]) for i in range(0, len(verts), 3)]
        grouped_norms = [(norms[i], norms[i + 1], norms[i + 2]) for i in range(0, len(norms), 3)]

        # USD の faceVarying 補間に合わせて index 順へ並べ替える
        grouped_norms = [grouped_norms[f] for f in indices]

        material_name: Optional[str] = None
        diffuse_color = (0, 0, 0)
        shape_materials = shape.geometry.materials
        if shape_materials:
            # マテリアルは1つと仮定する
            for mat in shape_materials:
                material_name = material_names.resolve(mat.name)
                diffuse_color = _color_to_tuple(mat.diffuse)
                if material_name in materials:
                    continue
                transparency = mat.transparency if mat.has_transparency() else None
                # IfcWindow は透過させる
                if element.is_a("IfcWindow"):
                    transparency = 0.8
                materials[material_name] = MaterialSpec(diffuse_color, transparency)

        yield grouped_verts, indices, grouped_norms, info, material_name, diffuse_color, matrix

        if not iterator.next():
            break


def get_space_geometry(settings, ifc_file, y_up: bool = False) -> Iterator[tuple]:
    """IfcSpaceのワールド座標ジオメトリを1件ずつ生成する（ジェネレータ、E9-5の
    先行タスク）。

    `get_geometry()`は空間系エレメント（`_EXCLUDED_TYPES`、IfcSpace含む）を
    除外するため、空間ボクセルヒートマップ向けに別経路を用意する。正本USD/GLB
    には一切影響しない（voxels.json/sdf.json/twin.jsonと同じ「付加的アセット」
    設計原則、`space_heatmap.py`が読む専用の経路）。

    USDのXform階層は経由せず、この関数自身でローカル→ワールド変換を適用した
    頂点を直接返す——`usd.append_prim`が`geometries`dict由来の頂点へ行う
    `rotation.dot(vert) + offset`と同じ変換（ifcopenshellのshapeはローカル座標の
    頂点とワールド変換行列を分けて返すため、USD Xform階層を経由せずここで
    直接ワールド座標を合成できる）。

    既知の限界（`append_prim`/`get_geometry()`から継承）: `y_up=True`のY/Z
    入れ替えはワールド変換前のローカル頂点にのみ適用し、回転行列自体は
    Z-UPのまま組み合わせるため、Y/Z入れ替えと可換でない回転（0°/180°ヨー
    以外）を持つ要素では正しいY-UPワールド座標にならない。これは`get_geometry()`
    側で元から存在する変換の組み方であり、この関数はそれを忠実に再現している
    だけなので、ここだけを直す修正はしない（直すなら`get_geometry()`/
    `append_prim`を含むパイプライン全体の見直しが必要）。

    Args:
        settings: ifcopenshell のジオメトリ設定
        ifc_file: 対象の IFC ファイル
        y_up: True で Y-UP、False で Z-UP（IFC 既定、`get_geometry()`と同じ規約）

    Yields:
        (guid, name, world_verts, indices) — world_vertsはワールド座標
        `(x, y, z)`タプルの列、indicesは三角形の頂点インデックス（flat, 3個ずつ組）。
    """
    iterator = geom.iterator(settings, ifc_file, multiprocessing.cpu_count())

    if not iterator.initialize():
        return

    while True:
        shape = iterator.get()
        element = ifc_file.by_guid(shape.guid)

        if not element.is_a("IfcSpace"):
            if not iterator.next():
                break
            continue

        matrix = _matrix12(shape.transformation.matrix)
        rotation, offset = decompose_transform(matrix)

        verts = shape.geometry.verts
        indices = list(shape.geometry.faces)

        local_verts = np.asarray(verts, dtype=float).reshape(-1, 3)
        world = local_verts @ rotation.T + offset
        if y_up:
            # USD 側はルート Xform で回すが、空間ジオメトリはその木に入らないので自前で揃える
            world = zup_to_yup(world)
        world_verts = [tuple(p) for p in world.tolist()]

        yield shape.guid, element.Name, world_verts, indices

        if not iterator.next():
            break


def _property_value(prop, element):
    """IfcProperty から素の値を取り出す。取り出せない型・壊れた値は None を返す。"""
    if prop.is_a("IfcPropertyEnumeratedValue"):
        values = [v.wrappedValue for v in prop.EnumerationValues or []]
        if not values:
            return None
        return values[0] if len(values) == 1 else ", ".join(str(v) for v in values)

    try:
        return prop.NominalValue.wrappedValue
    except AttributeError as exc:
        # 数千件の同種プロパティでログを埋め尽くさないよう、型ごとに1度だけ知らせる
        key = (prop.is_a(), str(exc))
        if key not in _WARNED_PROPERTY_FAILURES:
            _WARNED_PROPERTY_FAILURES.add(key)
            logger.warning(
                "Skipping unreadable %s property %r on %s: %s (further occurrences are not logged)",
                prop.is_a(), getattr(prop, "Name", "?"), element.is_a(), exc,
            )
        return None


def get_properties(element) -> dict:
    """IFC オブジェクトからプロパティを抽出する。"""
    ret = dict(vars(element))

    if hasattr(element, "IsDefinedBy"):
        for rel in element.IsDefinedBy:
            if rel.is_a("IfcRelDefinesByProperties"):
                pset = rel.RelatingPropertyDefinition
                if pset.is_a("IfcPropertySet"):
                    for prop in pset.HasProperties:
                        value = _property_value(prop, element)
                        if value is not None:
                            ret[prop.Name] = value
                elif pset.is_a("IfcElementQuantity"):
                    for quantity in pset.Quantities or []:
                        value_attr = _QUANTITY_VALUE_ATTRS.get(quantity.is_a())
                        if value_attr is None:
                            logger.debug("Unhandled quantity type: %s", quantity.is_a())
                            continue
                        ret[quantity.Name.replace(" ", "_")] = getattr(quantity, value_attr)
                else:
                    logger.debug("Unhandled property set type: %s", pset.is_a())
            elif rel.is_a("IfcRelDefinesByType"):
                # TODO: クラス(Family)の定義
                pass
            else:
                logger.debug("Unhandled IsDefinedBy relation: %s", rel.is_a())

    # 主観で不要なプロパティを刈り込む
    for key in ("OwnerHistory", "CompositionType", "Representation", "ObjectPlacement", "Reference"):
        ret.pop(key, None)

    addr = ret.pop("BuildingAddress", None)
    if addr:
        ret["Address"] = addr.AddressLines[0]
    return ret
