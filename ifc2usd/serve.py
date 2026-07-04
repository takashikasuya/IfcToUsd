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

from pxr import Usd

from .gltf import export_gltf
from .scene_index import build_scene_json

logger = logging.getLogger("ifc2usd")

_VIEWER_ASSETS_DIR = Path(__file__).parent / "viewer"


def _copy_viewer_assets(dest: Path) -> None:
    for item in _VIEWER_ASSETS_DIR.iterdir():
        target = dest / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target)


def build_serve_directory(usd_path: Path, workdir: Path) -> Path:
    """USD から scene.json/GLB を生成し、静的ビューワーアセットと共に
    `workdir` へ配置する。`workdir` は既存の空ディレクトリを想定する。
    """
    stage = Usd.Stage.Open(str(usd_path))

    glb_name = f"{Path(usd_path).stem}.glb"
    export_gltf(stage, str(workdir / glb_name))

    scene = build_scene_json(stage, assets={"gltf": glb_name})
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
