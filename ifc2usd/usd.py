"""USD ステージの構築: メッシュ・マテリアル・空間階層の書き出し。

もとは ``IFC_to_USD.ipynb`` のセルに分散していた USD 生成ロジックを整理し、
OpenUSD の現行 API（``MaterialBindingAPI.Apply`` など）に合わせて更新したもの。
"""

from __future__ import annotations

import logging

import numpy as np
from pxr import Gf, Kind, Sdf, Usd, UsdGeom, UsdShade

from .voxel import VoxelElement

logger = logging.getLogger(__name__)

# 各エレメントprim配下でメッシュを保持する子primの名前。この規約は
# usd.py（作成元）/ gltf.py / scene_index.py の3箇所で参照されるため、
# ここを唯一の正本とする。
MESH_PRIM_NAME = "mesh"

# マテリアル配下の UsdPreviewSurface シェーダ prim の名前。usd.py / gltf.py が共有する
PBR_SHADER_NAME = "PBRShader"

# 空間エレメントの prim 名の基底。Site/Building は単一なら連番なしの従来名を保つ
_SPATIAL_PRIM_BASE = {
    "IfcSite": "Site",
    "IfcBuilding": "Building",
    "IfcBuildingStorey": "Storey",
    "IfcSpace": "Space",
}


def _aggregated_children(model):
    """IsDecomposedBy の全リレーションから子オブジェクトを列挙する（1本と仮定しない）。"""
    for rel in getattr(model, "IsDecomposedBy", None) or []:
        yield from rel.RelatedObjects


def _contained_elements(model):
    """ContainsElements の全リレーションから要素を列挙する。"""
    for rel in getattr(model, "ContainsElements", None) or []:
        yield from rel.RelatedElements


def _spatial_prim_name(model, siblings, props) -> str:
    cls = model.is_a()
    base = _SPATIAL_PRIM_BASE.get(cls, cls)
    if cls in ("IfcSite", "IfcBuilding") and sum(1 for s in siblings if s.is_a(cls)) == 1:
        return base
    return f"{base}_{props['id']}"


def create_materials(stage, materials: dict) -> dict:
    """マテリアル辞書から UsdPreviewSurface マテリアルを作成する。

    Returns:
        マテリアル名 -> UsdShade.Material の辞書
    """
    metallic = 0.0
    roughness = 1.0
    material_prims: dict = {}

    for name, spec in materials.items():
        diffuse, transparency = spec
        path = Sdf.Path(f"/Materials/{name}")
        mat = UsdShade.Material.Define(stage, path)
        shader = UsdShade.Shader.Define(stage, path.AppendChild(PBR_SHADER_NAME))
        shader.CreateIdAttr("UsdPreviewSurface")

        shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(
            Gf.Vec3f(diffuse[0], diffuse[1], diffuse[2])
        )
        shader.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(roughness)
        shader.CreateInput("metallic", Sdf.ValueTypeNames.Float).Set(metallic)

        if transparency:
            # IFC は transparency、UsdPreviewSurface は opacity なので変換する
            shader.CreateInput("opacity", Sdf.ValueTypeNames.Float).Set(1.0 - transparency)
            shader.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(0.0)

        mat.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(), "surface")
        material_prims[name] = mat

    return material_prims


def create_mesh(stage, path: str, geometry, material_prims: dict) -> None:
    """1 エレメント分のメッシュを USD 上に定義する。"""
    mesh = UsdGeom.Mesh.Define(stage, f"{path}/{MESH_PRIM_NAME}")
    mesh.CreatePointsAttr(geometry.vertices)
    mesh.CreateFaceVertexCountsAttr(geometry.faces)
    mesh.CreateFaceVertexIndicesAttr(geometry.indices)
    mesh.CreateExtentAttr(UsdGeom.PointBased(mesh).ComputeExtent(mesh.GetPointsAttr().Get()))

    # 法線を明示指定し、Catmull-Clark による再分割を無効化する
    mesh.CreateNormalsAttr(geometry.normals)
    mesh.CreateSubdivisionSchemeAttr("none")
    mesh.SetNormalsInterpolation(UsdGeom.Tokens.faceVarying)
    mesh.CreateDoubleSidedAttr(False)

    color = geometry.color
    mesh.GetDisplayColorAttr().Set([Gf.Vec3f(color[0], color[1], color[2])])

    mat = material_prims.get(geometry.material_name)
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
        from .ifc import decompose_transform  # 遅延 import で循環参照を避ける

        geom_data = geometries[guid]
        rotation, offset = decompose_transform(geom_data.transform)
        world = geom_data._replace(vertices=(np.asarray(geom_data.vertices) @ rotation.T).tolist())

        UsdGeom.XformCommonAPI(prim).SetTranslate(Gf.Vec3d(*offset))
        Usd.ModelAPI(prim).SetKind(Kind.Tokens.component)
        create_mesh(stage, str(prim.GetPath()), world, material_prims)
    return prim


def build_stage(ifc_file, geometries: dict, materials: dict, output_path: str, y_up: bool = False) -> None:
    """geometries / materials から USD ステージを構築し、ファイルへ書き出す。"""
    stage = Usd.Stage.CreateInMemory()
    # IfcOpenShell はジオメトリを常にメートルへ正規化して返すため 1.0 が正しい
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y if y_up else UsdGeom.Tokens.z)

    model_root = stage.DefinePrim("/IFC_Model", "Xform")
    Usd.ModelAPI(model_root).SetKind(Kind.Tokens.assembly)
    stage.SetDefaultPrim(model_root)
    if y_up:
        # ジオメトリは IFC の Z-UP のまま。ルートで 1 回だけ回して Y-UP へ揃える
        UsdGeom.XformCommonAPI(model_root).SetRotate(Gf.Vec3f(-90.0, 0.0, 0.0))

    material_prims = create_materials(stage, materials)

    from .ifc import get_length_unit_scale, get_properties  # 遅延 import で循環参照を避ける

    # 正規化前の記述単位は USD 側からは復元できないので記録しておく
    model_root.SetCustomDataByKey("ifcLengthUnitScale", get_length_unit_scale(ifc_file))

    placed: set[str] = set()

    def place(props, path):
        placed.add(props["GlobalId"])
        return append_prim(stage, props, path, geometries, material_prims)

    def proc_elements(model, prim):
        for element in _contained_elements(model):
            props = get_properties(element)
            elem_prim = place(props, f"{prim.GetPath()}/Element_{props['id']}")
            for obj_model in _aggregated_children(element):
                props = get_properties(obj_model)
                place(props, f"{elem_prim.GetPath()}/Object_{props['id']}")

    def proc_spatial(model, parent_prim, siblings):
        props = get_properties(model)
        prim = place(props, f"{parent_prim.GetPath()}/{_spatial_prim_name(model, siblings, props)}")
        proc_elements(model, prim)

        children = list(_aggregated_children(model))
        for child in children:
            proc_spatial(child, prim, children)

    project = ifc_file.by_type("IfcProject")
    roots = list(_aggregated_children(project[0])) if project else []
    if not roots:
        # IfcProject からの分解が無いモデルでも、空間ルートだけは拾い上げる
        roots = [s for s in ifc_file.by_type("IfcSpatialStructureElement") if not s.Decomposes]
    if not any(r.is_a("IfcSite") for r in roots):
        logger.warning("IfcSite が空間階層のルートに見つかりません。Site 階層を省略して構築します")

    for root in roots:
        proc_spatial(root, model_root, roots)

    unplaced = sorted(set(geometries) - placed)
    if unplaced:
        logger.warning(
            "空間階層に配置されていない要素が %d 件あります（ジオメトリは出力されません）: %s",
            len(unplaced),
            ", ".join(unplaced[:10]) + (" ..." if len(unplaced) > 10 else ""),
        )

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
            shader = UsdShade.Shader(stage.GetPrimAtPath(mat_path.AppendChild(PBR_SHADER_NAME)))
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
