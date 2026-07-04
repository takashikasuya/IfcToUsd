"""テスト共通のフィクスチャ/ヘルパー。

`tests/fixtures/minimal.ifc` を変換した USD ステージから、ワールド座標の
メッシュ頂点や customData を取り出す処理は複数のテストファイルで必要になる
ため、ここに集約する。
"""

from __future__ import annotations

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
