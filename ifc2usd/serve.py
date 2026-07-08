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
import urllib.parse
from pathlib import Path
from typing import Mapping, Sequence

from pxr import Usd, UsdGeom

from . import __version__
from .gltf import export_gltf
from .scene_index import build_scene_json
from .sdf_slice import build_sdf_slices_json
from .twin import TwinApiError
from .twin_proxy import TwinProxy
from .usd import elements_from_stage
from .voxel import build_voxel_json

logger = logging.getLogger("ifc2usd")

_VIEWER_ASSETS_DIR = Path(__file__).parent / "viewer"
_DEFAULT_VOXEL_SIZES: tuple[float, ...] = (0.5,)
_DEFAULT_SDF_SLICE_SIZE = 0.5


def _copy_viewer_assets(dest: Path) -> None:
    for item in _VIEWER_ASSETS_DIR.iterdir():
        target = dest / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target)


def build_serve_directory(
    usd_path: Path,
    workdir: Path,
    voxel_sizes: Sequence[float] = _DEFAULT_VOXEL_SIZES,
    sdf_slices: bool = False,
    twin: Mapping | None = None,
) -> Path:
    """USD から scene.json/GLB/voxels.json を生成し、静的ビューワーアセットと共に
    `workdir` へ配置する。`workdir` は既存の空ディレクトリを想定する。

    ボクセル化可能な要素（GUID+class customData 付きの mesh、`elements_from_stage`
    参照）が1つもない場合は voxels.json 自体を生成せず、`scene.json` の
    `assets` に `voxels` キーを含めない（GLB のみのメッシュ表示は成立するため、
    ここで打ち切る必要はない）。

    `sdf_slices=True` の場合、要素ごとのSDF水平スライス（E5-3、`sdf_slice.py`）を
    追加で計算し `<stem>_sdf.json` を生成する。voxels.json と異なり既定で無効：
    narrow-band SDF構築は要素ごとに追加の占有ボクセル化2回（表面/内部）を要し、
    通常の変換・閲覧フローには不要なコストのため、明示的に要求されたときだけ払う。

    `twin`（E9-3、digital-twin-spec.md §4.2）に`build_twin_json()`が返す辞書を
    渡すと`<stem>_twin.json`として焼き込み、`scene.json`の`assets.twin`から
    参照できるようにする。voxels.json/sdf.jsonと同じ「付加的アセット」の規約:
    値そのものは含めない静的マニフェストで、トークン/クレデンシャルは
    （`--twin twin-config.json`にのみ存在し）ここには一切現れない。

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

    if sdf_slices and any(len(el.vertices) for el in elements):
        sdf_name = f"{usd_path.stem}_sdf.json"
        sdf = build_sdf_slices_json(elements, size=_DEFAULT_SDF_SLICE_SIZE)
        (workdir / sdf_name).write_text(json.dumps(sdf, ensure_ascii=False), encoding="utf-8")
        assets["sdf"] = sdf_name

    if twin is not None:
        twin_name = f"{usd_path.stem}_twin.json"
        (workdir / twin_name).write_text(json.dumps(twin, ensure_ascii=False), encoding="utf-8")
        assets["twin"] = twin_name

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


class _TwinProxyHandler(_NoDirectoryListingHandler):
    """`_NoDirectoryListingHandler`に`/api/twin/*`プロキシを足した版（E9-3）。

    ホワイトリスト方式: `/api/twin/values`・`/api/twin/history`以外は静的配信へ
    フォールスルーする（制御APIはそもそも実装しない=中継されない、という設計）。
    """

    def __init__(self, *args, twin_proxy: TwinProxy, **kwargs) -> None:
        self.twin_proxy = twin_proxy
        super().__init__(*args, **kwargs)

    def do_GET(self) -> None:  # noqa: N802 - stdlibのオーバーライド
        parsed = urllib.parse.urlparse(self.path)
        qs = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}

        if parsed.path == "/api/twin/values":
            self._handle_values(qs)
        elif parsed.path == "/api/twin/history":
            self._handle_history(qs)
        else:
            super().do_GET()

    def _handle_values(self, qs: dict) -> None:
        metric = qs.get("metric")
        if not metric:
            self._respond_json(400, {"error": "metric query parameter is required"})
            return
        try:
            result = self.twin_proxy.get_values(metric)
        except TwinApiError as exc:
            # str(exc)にはビルOSのbase_url（+パス+クエリ）が含まれるため、
            # ブラウザへは一般的な文言のみ返し、詳細はサーバー側ログにのみ残す
            # （digital-twin-spec.md §6: ビルOSのURL・クレデンシャルはサーバー
            # 側の設定ファイルにのみ存在させる）。
            logger.warning("twin proxy: /api/twin/values?metric=%s failed: %s", metric, exc)
            self._respond_json(502, {"error": "upstream Building OS request failed"})
            return
        self._respond_json(200, result)

    def _handle_history(self, qs: dict) -> None:
        point_id = qs.get("pointId")
        start = qs.get("start")
        end = qs.get("end")
        if not point_id or not start or not end:
            self._respond_json(400, {"error": "pointId, start, end query parameters are required"})
            return
        granularity = qs.get("granularity", "None")
        try:
            result = self.twin_proxy.get_history(point_id, start=start, end=end, granularity=granularity)
        except TwinApiError as exc:
            logger.warning("twin proxy: /api/twin/history?pointId=%s failed: %s", point_id, exc)
            self._respond_json(502, {"error": "upstream Building OS request failed"})
            return
        self._respond_json(200, result)

    def _respond_json(self, status: int, payload) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def make_server(
    directory: Path,
    port: int = 8000,
    twin_proxy: TwinProxy | None = None,
) -> http.server.ThreadingHTTPServer:
    """`directory` を静的配信する HTTP サーバーを構築する（起動はしない）。

    127.0.0.1 にのみバインドする（外部ネットワークからの意図しない
    アクセスを避けるため）。`port=0` を渡すと OS が空きポートを選ぶ。

    `twin_proxy`が与えられた場合のみ`/api/twin/*`プロキシを追加する
    （既定`None`のときは既存の静的配信ハンドラと完全に同一——twinアセットが
    無い場合の既存ビューワー機能の無変化を保つ）。
    """
    if twin_proxy is not None:
        handler = functools.partial(_TwinProxyHandler, directory=str(directory), twin_proxy=twin_proxy)
    else:
        handler = functools.partial(_NoDirectoryListingHandler, directory=str(directory))
    return http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
