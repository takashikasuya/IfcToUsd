"""`ifc2usd serve`: 変換済み USD ステージを Web ビューワーとしてローカル配信する。

`docs/viewer/spec.md` §1.2, §4.1 に対応する。ディレクトリ組み立て
（`build_serve_directory`）と HTTP サーバー起動（`make_server`）を分離し、
サーバーを実際に起動せずにディレクトリ組み立てだけを単体テストできるようにする。
"""

from __future__ import annotations

import functools
import http.server
import json
import logging
import shutil
from pathlib import Path
from typing import Sequence

from pxr import Usd, UsdGeom

from . import __version__
from .gltf import export_gltf
from .scene_index import build_scene_json
from .usd import elements_from_stage
from .voxel import build_voxel_json

logger = logging.getLogger("ifc2usd")

_VIEWER_ASSETS_DIR = Path(__file__).parent / "viewer"
_DEFAULT_VOXEL_SIZES: tuple[float, ...] = (0.5,)


def _copy_viewer_assets(dest: Path) -> None:
    for item in _VIEWER_ASSETS_DIR.iterdir():
        target = dest / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target)


def build_serve_directory(
    usd_path: Path, workdir: Path, voxel_sizes: Sequence[float] = _DEFAULT_VOXEL_SIZES
) -> Path:
    """USD から scene.json/GLB/voxels.json を生成し、静的ビューワーアセットと共に
    `workdir` へ配置する。`workdir` は既存の空ディレクトリを想定する。

    ボクセル化可能な要素（GUID+class customData 付きの mesh、`elements_from_stage`
    参照）が1つもない場合は voxels.json 自体を生成せず、`scene.json` の
    `assets` に `voxels` キーを含めない（GLB のみのメッシュ表示は成立するため、
    ここで打ち切る必要はない）。

    Raises:
        FileNotFoundError: `usd_path` が存在しない場合。CLI(`serve`)は事前に
            チェックして分かりやすいエラーにしているが、この関数を直接呼ぶ
            他の呼び出し元のために、生の Usd.Stage.Open 由来の pxr.Tf.ErrorException
            より分かりやすい例外にする。
    """
    usd_path = Path(usd_path)
    if not usd_path.is_file():
        raise FileNotFoundError(f"USD file not found: {usd_path}")

    stage = Usd.Stage.Open(str(usd_path))

    glb_name = f"{usd_path.stem}.glb"
    export_gltf(stage, str(workdir / glb_name))

    assets = {"gltf": glb_name}
    elements = elements_from_stage(stage)
    # elements_from_stage は customData(GUID/class) と mesh子prim があれば要素を
    # 返すが、頂点0件の退化メッシュも含みうる。全要素が頂点0件だと
    # build_voxel_json -> scene_origin が ValueError で落ちてしまうため、
    # 頂点を持つ要素が1つもなければ voxels.json 自体を省略する。
    if any(len(el.vertices) for el in elements):
        voxels_name = f"{usd_path.stem}_voxels.json"
        voxels = build_voxel_json(
            elements,
            sizes=voxel_sizes,
            source={"usd": usd_path.name, "generator": f"ifc2usd {__version__}"},
            up_axis=str(UsdGeom.GetStageUpAxis(stage)),
        )
        (workdir / voxels_name).write_text(json.dumps(voxels, ensure_ascii=False), encoding="utf-8")
        assets["voxels"] = voxels_name

    scene = build_scene_json(stage, assets=assets)
    (workdir / "scene.json").write_text(json.dumps(scene, ensure_ascii=False), encoding="utf-8")

    _copy_viewer_assets(workdir)
    return workdir


class _NoDirectoryListingHandler(http.server.SimpleHTTPRequestHandler):
    """ディレクトリ一覧表示を無効化した静的ファイルハンドラ。

    `vendor/` 配下のようにindex.htmlを持たないディレクトリでも、
    標準の SimpleHTTPRequestHandler はファイル名一覧を返してしまう。
    このツールが配信するのはユーザー自身のIFCから生成したファイルのみだが、
    意図しない一覧公開を避けるため一律 404 にする。
    """

    def list_directory(self, path):  # noqa: D102 - stdlibのオーバーライド
        self.send_error(404, "No permission to list directory")
        return None


def make_server(directory: Path, port: int = 8000) -> http.server.ThreadingHTTPServer:
    """`directory` を静的配信する HTTP サーバーを構築する（起動はしない）。

    127.0.0.1 にのみバインドする（外部ネットワークからの意図しない
    アクセスを避けるため）。`port=0` を渡すと OS が空きポートを選ぶ。
    """
    handler = functools.partial(_NoDirectoryListingHandler, directory=str(directory))
    return http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
