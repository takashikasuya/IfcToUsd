"""USD ステージから Web ビューワー用シーン記述 `scene.json` を抽出する。

`docs/viewer/spec.md` §4.1 に対応する。Web ビューワーはこの JSON を入口にし、
USD を直接パースしない想定のため、階層ツリーと customData をここで平坦化する。
"""

from __future__ import annotations

from typing import Optional

from pxr import Usd, UsdGeom

from .usd import MESH_PRIM_NAME

_SCHEMA_VERSION = 1

# usd.py の set_custom_data が実際に書き込むキーのみを通す。prim.GetCustomData()は
# これに加えてUSDスキーマ登録済みのdocumentation由来customData（例:
# "userDocBrief"）まで合成して返してくるため、素通しするとIFCと無関係な
# スキーマ説明文がプロパティパネルに漏れてしまう。
# usd.py の set_custom_data() がキーを追加/変更したら、ここも合わせて更新すること。
_METADATA_KEYS = ("GUID", "class", "Name", "LongName", "Description", "Latitude", "Longitude")


def _node_color(prim: Usd.Prim) -> Optional[list]:
    """要素のdisplayColor(usd.pyがmesh prim作成時に書き込む、tests/test_convert.py
    のEXPECTED_WALLSと同じ値)をツリー色チップ(E8-3/Issue #44)用に返す。
    Site/Building/Storeyのようにmesh子primを持たないノードはNone
    （scene.jsonスキーマの後方互換: 無ければビューワー側でチップ非表示）。
    """
    mesh_prim = prim.GetChild(MESH_PRIM_NAME)
    if not mesh_prim.IsValid():
        return None
    display_color = UsdGeom.Mesh(mesh_prim).GetDisplayColorAttr().Get()
    if not display_color:
        return None
    c = display_color[0]
    return [c[0], c[1], c[2]]


def _tree_node(prim: Usd.Prim) -> dict:
    all_custom_data = prim.GetCustomData()
    cd = {key: all_custom_data[key] for key in _METADATA_KEYS if key in all_custom_data}
    children = [
        _tree_node(child) for child in prim.GetChildren() if child.GetName() != MESH_PRIM_NAME
    ]
    return {
        "path": str(prim.GetPath()),
        "name": cd.get("Name"),
        "class": cd.get("class"),
        "guid": cd.get("GUID"),
        "color": _node_color(prim),
        "customData": cd,
        "children": children,
    }


def build_scene_json(stage: Usd.Stage, assets: Optional[dict] = None) -> dict:
    """USD ステージから spec.md §4.1 の scene.json を構築する。

    `tree` の最上位はステージのデフォルトprim自体（例: `/IFC_Model`)ではなく
    その子（例: `/IFC_Model/Site`）から始まる。デフォルトprimは意味情報を
    持たないassemblyコンテナに過ぎないため。

    Raises:
        ValueError: ステージにデフォルトprimが設定されていない場合
            （`serve` のように任意のUSDファイルを受け付ける経路で、
            defaultPrim未設定のファイルを渡された際に生の RuntimeError
            ではなく分かりやすいエラーにするため）。
    """
    root = stage.GetDefaultPrim()
    if not root.IsValid():
        raise ValueError(f"stage has no default prim: {stage.GetRootLayer().identifier}")
    tree = [_tree_node(child) for child in root.GetChildren() if child.GetName() != MESH_PRIM_NAME]

    up_axis = UsdGeom.GetStageUpAxis(stage)

    return {
        "version": _SCHEMA_VERSION,
        "upAxis": str(up_axis),
        "assets": assets or {},
        "tree": tree,
    }
