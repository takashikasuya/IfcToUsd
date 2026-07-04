"""USD ステージの構築: メッシュ・マテリアル・空間階層の書き出し。

もとは ``IFC_to_USD.ipynb`` のセルに分散していた USD 生成ロジックを整理し、
OpenUSD の現行 API（``MaterialBindingAPI.Apply`` など）に合わせて更新したもの。
"""

from __future__ import annotations

import copy
import logging

import numpy as np
from pxr import Gf, Kind, Sdf, Usd, UsdGeom, UsdShade

from .voxel import VoxelElement

logger = logging.getLogger(__name__)

# 各エレメントprim配下でメッシュを保持する子primの名前。この規約は
# usd.py（作成元）/ gltf.py / scene_index.py の3箇所で参照されるため、
# ここを唯一の正本とする。
MESH_PRIM_NAME = "mesh"


def create_materials(stage, materials: dict) -> dict:
    """マテリアル辞書から UsdPreviewSurface マテリアルを作成する。

    Returns:
        マテリアル名 -> UsdShade.Material の辞書
    """
    metallic = 0.0
    roughness = 1.0
    material_prims: dict = {}

    for name, (diffuse, specular, transparency) in materials.items():
        path = Sdf.Path(f"/Materials/{name}")
        mat = UsdShade.Material.Define(stage, path)
        shader = UsdShade.Shader.Define(stage, path.AppendChild("PBRShader"))
        shader.CreateIdAttr("UsdPreviewSurface")

        shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(
            Gf.Vec3f(diffuse[0], diffuse[1], diffuse[2])
        )
        shader.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(roughness)
        shader.CreateInput("metallic", Sdf.ValueTypeNames.Float).Set(metallic)

        if specular:
            shader.CreateInput("specularColor", Sdf.ValueTypeNames.Color3f).Set(
                Gf.Vec3f(specular[0], specular[1], specular[2])
            )
        if transparency:
            # IFC は transparency、UsdPreviewSurface は opacity なので変換する
            shader.CreateInput("opacity", Sdf.ValueTypeNames.Float).Set(1.0 - transparency)
            shader.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(0.0)

        mat.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(), "surface")
        material_prims[name] = mat

    return material_prims


def create_mesh(stage, path: str, geometry, material_prims: dict) -> None:
    """1 エレメント分のメッシュを USD 上に定義する。"""
    faces, vertices, indices, material_name, color, normals, _translate = geometry

    mesh = UsdGeom.Mesh.Define(stage, f"{path}/{MESH_PRIM_NAME}")
    mesh.CreatePointsAttr(vertices)
    mesh.CreateFaceVertexCountsAttr(faces)
    mesh.CreateFaceVertexIndicesAttr(indices)
    mesh.CreateExtentAttr(UsdGeom.PointBased(mesh).ComputeExtent(mesh.GetPointsAttr().Get()))

    # 法線を明示指定し、Catmull-Clark による再分割を無効化する
    mesh.CreateNormalsAttr(normals)
    mesh.CreateSubdivisionSchemeAttr("none")
    mesh.SetNormalsInterpolation(UsdGeom.Tokens.faceVarying)
    mesh.CreateDoubleSidedAttr(False)

    mesh.GetDisplayColorAttr().Set([Gf.Vec3f(color[0], color[1], color[2])])

    mat = material_prims.get(material_name)
    if mat is not None:
        UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim())
        UsdShade.MaterialBindingAPI(mesh).Bind(mat, UsdShade.Tokens.preview)


def set_custom_data(stage, prim, props: dict) -> None:
    """prim に IFC 由来のメタデータ（class/GUID/名称/緯度経度）を付与する。"""
    target = stage.GetPrimAtPath(prim.GetPath())
    target.SetCustomDataByKey("class", props["type"])
    target.SetCustomDataByKey("GUID", props["GlobalId"])

    for key in ("Name", "LongName", "Description"):
        if key in props and props[key] is not None:
            target.SetCustomDataByKey(key, props[key])

    if "RefLatitude" in props:
        lat = ".".join(str(i) for i in props["RefLatitude"]) if props["RefLatitude"] is not None else ""
        lon = ".".join(str(i) for i in props["RefLongitude"]) if props["RefLongitude"] is not None else ""
        target.SetCustomDataByKey("Latitude", lat)
        target.SetCustomDataByKey("Longitude", lon)


def append_prim(stage, props: dict, path: str, geometries: dict, material_prims: dict):
    """空間階層に Xform prim を追加し、対応するジオメトリがあればメッシュを配置する。"""
    prim = UsdGeom.Xform.Define(stage, path)
    Usd.ModelAPI(prim).SetKind(Kind.Tokens.group)
    set_custom_data(stage, prim, props)

    guid = props["GlobalId"]
    if guid in geometries:
        geom_data = copy.copy(geometries[guid])
        verts = geom_data[1]
        t = geom_data[6]
        # 変換行列 (3x3 回転 + 平行移動) を分解する
        rows = [(t[i], t[i + 1], t[i + 2]) for i in range(0, 12, 3)]
        offset = rows[3]
        # rows[0:3] は X/Y/Z 基底ベクトル。列に並べて回転行列とし各頂点へ適用する
        rotation = np.asarray((rows[0], rows[1], rows[2])).T
        geom_data[1] = [rotation.dot(vert).tolist() for vert in verts]

        UsdGeom.XformCommonAPI(prim).SetTranslate(offset)
        Usd.ModelAPI(prim).SetKind(Kind.Tokens.component)
        create_mesh(stage, str(prim.GetPath()), geom_data, material_prims)
    return prim


def build_stage(ifc_file, geometries: dict, materials: dict, output_path: str, y_up: bool = False) -> None:
    """geometries / materials から USD ステージを構築し、ファイルへ書き出す。"""
    stage = Usd.Stage.CreateInMemory()
    # シーンの単位を m にする
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y if y_up else UsdGeom.Tokens.z)

    model_root = stage.DefinePrim("/IFC_Model", "Xform")
    Usd.ModelAPI(model_root).SetKind(Kind.Tokens.assembly)
    stage.SetDefaultPrim(model_root)

    material_prims = create_materials(stage, materials)

    from .ifc import get_properties  # 遅延 import で循環参照を避ける

    def proc_elements(model, prim):
        for rel in model.ContainsElements:
            for element in rel.RelatedElements:
                props = get_properties(element)
                elem_prim = append_prim(
                    stage, props, f"{prim.GetPath()}/Element_{props['id']}", geometries, material_prims
                )
                if len(element.IsDecomposedBy) > 0:
                    for obj_model in element.IsDecomposedBy[0].RelatedObjects:
                        props = get_properties(obj_model)
                        append_prim(
                            stage, props, f"{elem_prim.GetPath()}/Object_{props['id']}", geometries, material_prims
                        )

    props = get_properties(ifc_file.by_type("IfcSite")[0])
    site = append_prim(stage, props, f"{model_root.GetPath()}/Site", geometries, material_prims)

    props = get_properties(ifc_file.by_type("IfcBuilding")[0])
    building = append_prim(stage, props, f"{site.GetPath()}/Building", geometries, material_prims)

    for storey_model in ifc_file.by_type("IfcBuildingStorey"):
        props = get_properties(storey_model)
        storey_prim = append_prim(
            stage, props, f"{building.GetPath()}/Storey_{props['id']}", geometries, material_prims
        )

        if storey_model.ContainsElements:
            proc_elements(storey_model, storey_prim)

        if len(storey_model.IsDecomposedBy) > 0:
            assert len(storey_model.IsDecomposedBy) == 1
            for space_model in storey_model.IsDecomposedBy[0].RelatedObjects:
                props = get_properties(space_model)
                space = append_prim(
                    stage, props, f"{storey_prim.GetPath()}/Space_{props['id']}", geometries, material_prims
                )
                if space_model.ContainsElements:
                    proc_elements(space_model, space)

    stage.Export(output_path)


def elements_from_stage(stage) -> list[VoxelElement]:
    """変換済み USD ステージから、ボクセル化対象の要素情報を抽出する。

    `append_prim` が付与する規約（customData の GUID/class/Name、子 prim
    "mesh"、UsdPreviewSurface の diffuseColor バインディング）に依存する。
    """
    elements: list[VoxelElement] = []
    for prim in stage.Traverse():
        cd = prim.GetCustomData()
        if "GUID" not in cd or "class" not in cd:
            continue

        mesh_prim = stage.GetPrimAtPath(prim.GetPath().AppendChild(MESH_PRIM_NAME))
        if not mesh_prim.IsValid():
            continue

        mesh = UsdGeom.Mesh(mesh_prim)
        xform = UsdGeom.Xformable(mesh_prim).ComputeLocalToWorldTransform(Usd.TimeCode.Default())
        points = mesh.GetPointsAttr().Get() or []
        vertices = [tuple(xform.Transform(Gf.Vec3d(*p))) for p in points]
        indices = list(mesh.GetFaceVertexIndicesAttr().Get() or [])

        color = (0.0, 0.0, 0.0)
        mat_path = UsdShade.MaterialBindingAPI(mesh).GetDirectBinding().GetMaterialPath()
        if mat_path:
            shader = UsdShade.Shader(stage.GetPrimAtPath(mat_path.AppendChild("PBRShader")))
            diffuse = shader.GetInput("diffuseColor").Get()
            if diffuse is not None:
                color = (diffuse[0], diffuse[1], diffuse[2])

        elements.append(
            VoxelElement(
                guid=cd["GUID"],
                cls=cd["class"],
                name=cd.get("Name"),
                color=color,
                vertices=vertices,
                indices=indices,
            )
        )
    return elements
