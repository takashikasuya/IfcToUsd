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


def _mesh_diffuse_color(mesh: UsdGeom.Mesh, stage: Usd.Stage) -> tuple[float, float, float]:
    mat_path = UsdShade.MaterialBindingAPI(mesh).GetDirectBinding().GetMaterialPath()
    if not mat_path:
        return _DEFAULT_COLOR
    shader = UsdShade.Shader(stage.GetPrimAtPath(mat_path.AppendChild("PBRShader")))
    diffuse = shader.GetInput("diffuseColor").Get()
    if diffuse is None:
        return _DEFAULT_COLOR
    return (diffuse[0], diffuse[1], diffuse[2])


def _mesh_to_trimesh(mesh_prim: Usd.Prim, stage: Usd.Stage) -> trimesh.Trimesh:
    mesh = UsdGeom.Mesh(mesh_prim)
    points = mesh.GetPointsAttr().Get() or []
    vertices = np.array([(p[0], p[1], p[2]) for p in points], dtype=np.float64)
    indices = np.array(mesh.GetFaceVertexIndicesAttr().Get() or [], dtype=np.int64).reshape(-1, 3)

    tri_mesh = trimesh.Trimesh(vertices=vertices, faces=indices, process=False)

    color = _mesh_diffuse_color(mesh, stage)
    pbr = trimesh.visual.material.PBRMaterial(baseColorFactor=[color[0], color[1], color[2], 1.0])
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
