"""IFC の読み取り: ジオメトリ抽出とプロパティ抽出。

もとは ``IFC_to_USD.ipynb`` のセルに分散していたロジックを、実行順に依存しない
純粋な関数群として再構成したもの。ifcopenshell 0.8 系の API に対応する。
"""

from __future__ import annotations

import logging
import multiprocessing
import re
from typing import Iterator, Optional

import ifcopenshell
from ifcopenshell import geom

logger = logging.getLogger(__name__)

# ジオメトリから除外する空間系エレメント（開口部・空間・ゾーン）
_EXCLUDED_TYPES = ("IfcOpeningElement", "IfcSpace", "IfcSpatialZone")

# マテリアル名として使えない文字を除去する
_MATERIAL_NAME_RE = re.compile(r"[-<>/,()]")


def sanitize_material_name(name: str) -> str:
    """マテリアル名を USD の prim 名として有効な文字列へ整形する。"""
    # ハイフンはアンダースコアへ、その他の記号は削除する
    return _MATERIAL_NAME_RE.sub(lambda m: "_" if m.group() == "-" else "", name)


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


def get_project_info(ifc_file, name: str = "Sample") -> tuple[str, str, str]:
    """IfcProject / IfcSite からプロジェクト名と緯度経度を取得する。"""
    prj = ifc_file.by_type("IfcProject")[0]
    name_ = prj.LongName if prj.LongName != "プロジェクト名" else name
    name_ = name if name_ is None else name_

    site = ifc_file.by_type("IfcSite")[0]
    lat = ".".join(str(i) for i in site.RefLatitude) if site.RefLatitude is not None else ""
    lon = ".".join(str(i) for i in site.RefLongitude) if site.RefLongitude is not None else ""
    return name_, lat, lon


def get_geometry(settings, ifc_file, materials: dict, y_up: bool = False) -> Iterator[tuple]:
    """対象オブジェクトのジオメトリを1件ずつ生成する（ジェネレータ）。

    Args:
        settings: ifcopenshell のジオメトリ設定
        ifc_file: 対象の IFC ファイル
        materials: マテリアル名→(diffuse, specular, transparency) を蓄積する辞書
        y_up: True で Y-UP、False で Z-UP（IFC 既定）
    """
    iterator = geom.iterator(settings, ifc_file, multiprocessing.cpu_count())

    if not iterator.initialize():
        return

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

        # Y-UP のときは Y/Z を入れ替える
        if not y_up:
            grouped_verts = [(verts[i], verts[i + 1], verts[i + 2]) for i in range(0, len(verts), 3)]
            grouped_norms = [(norms[i], norms[i + 1], norms[i + 2]) for i in range(0, len(norms), 3)]
        else:
            grouped_verts = [(verts[i], verts[i + 2], verts[i + 1]) for i in range(0, len(verts), 3)]
            grouped_norms = [(norms[i], norms[i + 2], norms[i + 1]) for i in range(0, len(norms), 3)]

        # USD の faceVarying 補間に合わせて index 順へ並べ替える
        grouped_norms = [grouped_norms[f] for f in indices]

        material_name: Optional[str] = None
        diffuse_color = (0, 0, 0)
        shape_materials = shape.geometry.materials
        if shape_materials:
            # マテリアルは1つと仮定する
            for mat in shape_materials:
                material_name = sanitize_material_name(mat.name)
                diffuse_color = _color_to_tuple(mat.diffuse)
                if material_name in materials:
                    continue
                specular_color = _color_to_tuple(mat.specular)
                transparency = mat.transparency if mat.has_transparency() else None
                # IfcWindow は透過させる
                if element.is_a("IfcWindow"):
                    transparency = 0.8
                materials[material_name] = (diffuse_color, specular_color, transparency)

        yield grouped_verts, indices, grouped_norms, info, material_name, diffuse_color, matrix

        if not iterator.next():
            break


def get_properties(element) -> dict:
    """IFC オブジェクトからプロパティを抽出する。"""
    ret = dict(vars(element))

    if hasattr(element, "IsDefinedBy"):
        for rel in element.IsDefinedBy:
            if rel.is_a("IfcRelDefinesByProperties"):
                pset = rel.RelatingPropertyDefinition
                if pset.is_a("IfcPropertySet"):
                    for prop in pset.HasProperties:
                        try:
                            ret[prop.Name] = prop.NominalValue.wrappedValue
                        except Exception:
                            logger.debug("Invalid property on %s", getattr(prop, "Name", "?"))
                elif pset.is_a("IfcElementQuantity"):
                    quantities = pset.Quantities[0]
                    if quantities.is_a("IfcQuantityArea"):
                        label = quantities.Name.replace(" ", "_")
                        ret[label] = quantities.AreaValue
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
