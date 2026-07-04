"""USD ステージを glTF(GLB) へエクスポートする。

`docs/viewer/spec.md` §4.1 の `scene.json` が GUID で結合するため、各ノードの
`extras` に `guid`/`class`/`name` を必ず付与する。座標系・マテリアル変換は
`IFC_to_GLTF.ipynb` の知見（trimesh の Scene グラフに階層を積み上げ、
マテリアルの diffuse を反映する）を踏まえるが、入力は IFC ではなく
`ifc2usd convert` で構築済みの USD ステージそのもの。
"""

from __future__ import annotations

import numpy as np
import trimesh
from pxr import Usd, UsdGeom, UsdShade

_MESH_CHILD_NAME = "mesh"
_DEFAULT_COLOR = (0.5, 0.5, 0.5)


def _local_matrix(prim: Usd.Prim) -> np.ndarray:
    """prim自身の親相対ローカル変換行列を、trimesh/numpyの列ベクトル規約で返す。

    Gf.Matrix4d は行ベクトル規約（並進が最終行）のため転置する。
    """
    xformable = UsdGeom.Xformable(prim)
    if not xformable:
        return np.eye(4)
    return np.array(xformable.GetLocalTransformation()).T


def _node_metadata(prim: Usd.Prim) -> dict:
    cd = prim.GetCustomData()
    metadata = {}
    if "GUID" in cd:
        metadata["guid"] = cd["GUID"]
    if "class" in cd:
        metadata["class"] = cd["class"]
    if cd.get("Name") is not None:
        metadata["name"] = cd["Name"]
    return metadata


def _mesh_display_color(mesh: UsdGeom.Mesh) -> tuple[float, float, float]:
    colors = mesh.GetDisplayColorAttr().Get()
    if colors:
        c = colors[0]
        return (c[0], c[1], c[2])
    return _DEFAULT_COLOR


def _mesh_color_and_opacity(mesh: UsdGeom.Mesh, stage: Usd.Stage) -> tuple[float, float, float, float]:
    """バインドされたマテリアルの diffuseColor/opacity を返す。

    マテリアル未バインドの場合は、usd.py の create_mesh が常に設定する
    displayColor にフォールバックする（灰色決め打ちにはしない）。
    """
    mat_path = UsdShade.MaterialBindingAPI(mesh).GetDirectBinding().GetMaterialPath()
    if not mat_path:
        r, g, b = _mesh_display_color(mesh)
        return (r, g, b, 1.0)

    shader = UsdShade.Shader(stage.GetPrimAtPath(mat_path.AppendChild("PBRShader")))
    diffuse = shader.GetInput("diffuseColor").Get()
    if diffuse is None:
        r, g, b = _mesh_display_color(mesh)
    else:
        r, g, b = diffuse[0], diffuse[1], diffuse[2]

    opacity_input = shader.GetInput("opacity")
    opacity = opacity_input.Get() if opacity_input else None
    alpha = float(opacity) if opacity is not None else 1.0
    return (r, g, b, alpha)


def _mesh_to_trimesh(mesh_prim: Usd.Prim, stage: Usd.Stage) -> trimesh.Trimesh:
    mesh = UsdGeom.Mesh(mesh_prim)
    points = mesh.GetPointsAttr().Get() or []
    point_arr = np.array([(p[0], p[1], p[2]) for p in points], dtype=np.float64)
    indices = np.array(mesh.GetFaceVertexIndicesAttr().Get() or [], dtype=np.int64)

    # 法線は face-varying（indices と同じ長さ、faceVertexIndices 順）で格納されて
    # おり、points（重複除去された頂点位置）とは長さが一致しない。points を
    # indices で展開（面-corner ごとに複製）することで、法線と1:1に揃える。
    exploded_vertices = point_arr[indices]
    faces = np.arange(len(indices)).reshape(-1, 3)

    tri_mesh = trimesh.Trimesh(vertices=exploded_vertices, faces=faces, process=False)

    normals = mesh.GetNormalsAttr().Get()
    if normals and len(normals) == len(exploded_vertices):
        tri_mesh.vertex_normals = np.array([(n[0], n[1], n[2]) for n in normals], dtype=np.float64)

    r, g, b, alpha = _mesh_color_and_opacity(mesh, stage)
    pbr = trimesh.visual.material.PBRMaterial(
        baseColorFactor=[r, g, b, alpha],
        alphaMode="BLEND" if alpha < 1.0 else "OPAQUE",
    )
    tri_mesh.visual = trimesh.visual.TextureVisuals(material=pbr)
    return tri_mesh


def _add_node(stage: Usd.Stage, prim: Usd.Prim, parent_node_name: str, scene: trimesh.Scene) -> None:
    node_name = prim.GetName()
    local_matrix = _local_matrix(prim)
    metadata = _node_metadata(prim)

    mesh_prim = stage.GetPrimAtPath(prim.GetPath().AppendChild(_MESH_CHILD_NAME))
    if mesh_prim.IsValid():
        tri_mesh = _mesh_to_trimesh(mesh_prim, stage)
        scene.add_geometry(
            tri_mesh,
            node_name=node_name,
            geom_name=node_name,
            parent_node_name=parent_node_name,
            transform=local_matrix,
            metadata=metadata,
        )
    else:
        scene.graph.update(
            frame_to=node_name, frame_from=parent_node_name, matrix=local_matrix, metadata=metadata
        )

    for child in prim.GetChildren():
        if child.GetName() == _MESH_CHILD_NAME:
            continue
        _add_node(stage, child, node_name, scene)


def export_gltf(stage: Usd.Stage, output_path: str) -> str:
    """USD ステージを glTF(GLB) へエクスポートし、書き出し先パスを返す。"""
    scene = trimesh.Scene()
    root = stage.GetDefaultPrim()
    _add_node(stage, root, scene.graph.base_frame, scene)
    scene.export(str(output_path))
    return str(output_path)
