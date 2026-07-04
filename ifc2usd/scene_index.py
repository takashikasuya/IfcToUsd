"""USD ステージから Web ビューワー用シーン記述 `scene.json` を抽出する。

`docs/viewer/spec.md` §4.1 に対応する。Web ビューワーはこの JSON を入口にし、
USD を直接パースしない想定のため、階層ツリーと customData をここで平坦化する。
"""

from __future__ import annotations

from typing import Optional

from pxr import Usd, UsdGeom

from .usd import MESH_PRIM_NAME

_SCHEMA_VERSION = 1


def _tree_node(prim: Usd.Prim) -> dict:
    cd = dict(prim.GetCustomData())
    children = [
        _tree_node(child) for child in prim.GetChildren() if child.GetName() != MESH_PRIM_NAME
    ]
    return {
        "path": str(prim.GetPath()),
        "name": cd.get("Name"),
        "class": cd.get("class"),
        "guid": cd.get("GUID"),
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
