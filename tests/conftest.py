"""テスト共通のフィクスチャ/ヘルパー。

`tests/fixtures/minimal.ifc` を変換した USD ステージから、ワールド座標の
メッシュ頂点や customData を取り出す処理は複数のテストファイルで必要になる
ため、ここに集約する。
"""

from __future__ import annotations

import http.server
import json
import threading
import urllib.parse
from pathlib import Path

import pytest
from pxr import Gf, Usd, UsdGeom, UsdShade

from ifc2usd import convert

FIXTURE = Path(__file__).parent / "fixtures" / "minimal.ifc"

# このサンドボックス環境ではpip配布のplaywrightパッケージが期待するブラウザリビジョンと
# 事前インストール済みのChromiumが一致しないため、固定パスの起動が必要
# （`playwright install`はこの環境のポリシー上実行できない）。他の環境/CIでは
# このパスが存在しないことがあるため、存在する場合のみ指定し、なければ
# Playwright標準のバンドル済みブラウザにフォールバックする。
_PINNED_CHROMIUM_PATH = Path("/opt/pw-browsers/chromium")

CHROMIUM_LAUNCH_ARGS = ["--use-gl=swiftshader", "--enable-webgl", "--ignore-gpu-blocklist"]


def chromium_launch_kwargs() -> dict:
    """Playwrightの`browser_type.launch()`へ渡すkwargsを返す。"""
    kwargs: dict = {"args": CHROMIUM_LAUNCH_ARGS}
    if _PINNED_CHROMIUM_PATH.is_file():
        kwargs["executable_path"] = str(_PINNED_CHROMIUM_PATH)
    return kwargs


@pytest.fixture(scope="module")
def stage(tmp_path_factory) -> Usd.Stage:
    out = tmp_path_factory.mktemp("usd") / "minimal.usda"
    convert(FIXTURE, out)
    return Usd.Stage.Open(str(out))


def world_mesh(stage: Usd.Stage, mesh_path: str) -> tuple[list[tuple[float, float, float]], list[int]]:
    """USD メッシュの points をワールド座標へ変換し、(vertices, indices) を返す。"""
    prim = stage.GetPrimAtPath(mesh_path)
    mesh = UsdGeom.Mesh(prim)
    xform = UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(Usd.TimeCode.Default())
    points = [xform.Transform(Gf.Vec3d(*p)) for p in mesh.GetPointsAttr().Get()]
    vertices = [(p[0], p[1], p[2]) for p in points]
    indices = list(mesh.GetFaceVertexIndicesAttr().Get())
    return vertices, indices


def mesh_diffuse_color(stage: Usd.Stage, mesh_path: str) -> tuple[float, float, float]:
    """メッシュにバインドされたマテリアルの diffuseColor を取得する。"""
    mesh = UsdGeom.Mesh(stage.GetPrimAtPath(mesh_path))
    mat_path = UsdShade.MaterialBindingAPI(mesh).GetDirectBinding().GetMaterialPath()
    shader = UsdShade.Shader(stage.GetPrimAtPath(mat_path.AppendChild("PBRShader")))
    color = shader.GetInput("diffuseColor").Get()
    return (color[0], color[1], color[2])


def wall_mesh_path(stage: Usd.Stage, name: str) -> str:
    for prim in stage.Traverse():
        cd = prim.GetCustomData()
        if cd.get("class") == "IfcWall" and cd.get("Name") == name:
            return str(prim.GetPath().AppendChild("mesh"))
    raise AssertionError(f"wall not found: {name}")


# --- ビルOS（GUTP Building OS RI）モックHTTPサーバー (Epic E9) ---
#
# `docs/viewer/digital-twin-spec.md` §2 に記載の一次ソース確認済みペイロード形を
# そのまま返す。実インスタンス・docker-compose.oss.yamlに依存せず、E9系
# （twin.py アダプタ、serve --twin プロキシ等）のテストがオフラインで走るための
# 共有フィクスチャ。

TWIN_BUILDINGS = [{"dtId": "building-1", "name": "Test Building"}]
TWIN_FLOORS = {"building-1": [{"dtId": "floor-1", "name": "1F"}]}
TWIN_SPACES = {"floor-1": [{"dtId": "space-1", "name": "Room 101"}]}
TWIN_DEVICES = {"space-1": [{"dtId": "device-1", "name": "Sensor 1"}]}
TWIN_POINTS = {
    "device-1": [
        {"dtId": "point-temp-1", "name": "Temperature"},
        {"dtId": "point-co2-1", "name": "CO2"},
    ]
}
TWIN_LATEST = {
    "point-temp-1": {
        "pointId": "point-temp-1",
        "value": 23.4,
        "datetime": "2026-07-08T09:00:00Z",
        "unit": "celsius",
    },
    "point-co2-1": {
        "pointId": "point-co2-1",
        "value": 512,
        "datetime": "2026-07-08T09:00:00Z",
        "unit": "ppm",
    },
}
TWIN_HISTORY = {
    "point-temp-1": [
        {"datetime": "2026-07-08T08:00:00Z", "value": 22.9},
        {"datetime": "2026-07-08T09:00:00Z", "value": 23.4},
    ],
}
TWIN_RESOURCES_BY_CUSTOM_TAG = {
    "guid:2AeZbGoSL7": [{"dtId": "point-temp-1", "customTags": ["guid:2AeZbGoSL7"]}],
}
TWIN_RESOURCES_BY_QUERY = {
    "temperature": [{"dtId": "point-temp-1", "name": "Temperature"}],
}


class _TwinMockHandler(http.server.BaseHTTPRequestHandler):
    last_authorization_header: str | None = None

    def log_message(self, format, *args):  # noqa: D102 - stdlibのオーバーライド、テスト出力を静める
        pass

    def _respond(self, status: int, payload) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlibのオーバーライド
        type(self).last_authorization_header = self.headers.get("Authorization")
        parsed = urllib.parse.urlparse(self.path)
        qs = {
            k: v[0]
            for k, v in urllib.parse.parse_qs(parsed.query, keep_blank_values=True).items()
        }
        path = parsed.path

        if path == "/api/buildings":
            self._respond(200, TWIN_BUILDINGS)
        elif path == "/api/floors":
            self._respond(200, TWIN_FLOORS.get(qs.get("buildingDtId"), []))
        elif path == "/api/spaces":
            self._respond(200, TWIN_SPACES.get(qs.get("floorDtId"), []))
        elif path == "/api/devices":
            self._respond(200, TWIN_DEVICES.get(qs.get("spaceDtId"), []))
        elif path == "/api/points":
            self._respond(200, TWIN_POINTS.get(qs.get("deviceDtId"), []))
        elif path == "/telemetries/query":
            point_id = qs.get("pointId")
            if point_id == "trigger-500":
                self._respond(500, {"error": "boom"})
            elif qs.get("latest") == "true":
                data = TWIN_LATEST.get(point_id)
                if data is None:
                    self._respond(404, {"error": "unknown point"})
                else:
                    self._respond(200, data)
            else:
                self._respond(200, TWIN_HISTORY.get(point_id, []))
        elif path == "/resources/search":
            if qs.get("customTags") is not None:
                self._respond(200, TWIN_RESOURCES_BY_CUSTOM_TAG.get(qs.get("customTags"), []))
            else:
                self._respond(200, TWIN_RESOURCES_BY_QUERY.get(qs.get("q"), []))
        else:
            self._respond(404, {"error": "unknown route"})


def get_last_twin_authorization_header() -> str | None:
    """`mock_twin_server`が直近に受け取ったリクエストの`Authorization`ヘッダを返す
    （token伝搬のテスト用の公開アクセサ。`_TwinMockHandler`自体はテストから直接
    触れない実装詳細として扱う）。"""
    return _TwinMockHandler.last_authorization_header


@pytest.fixture(scope="module")
def mock_twin_server():
    """`http://127.0.0.1:<port>` で digital-twin-spec.md §2 のペイロード形を返す
    モックビルOSサーバーを起動し、そのベースURLを返す。

    エンドポイントは全て読み取り専用の固定データを返すだけでテスト間の状態を
    持たないため、モジュール内のテストで1サーバーを使い回す
    （関数スコープ毎のbind/thread起動・shutdownコストを避ける）。"""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _TwinMockHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
