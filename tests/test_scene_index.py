"""scene_index.py（USD→scene.json）のテスト。

`tests/fixtures/minimal.ifc` を変換したUSDから、spec.md §4.1 の scene.json を
生成し、階層（Site→Building→Storey→Element）とcustomDataを検証する。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pxr import Usd

from ifc2usd import convert
from ifc2usd.scene_index import build_scene_json

FIXTURE = Path(__file__).parent / "fixtures" / "minimal.ifc"


@pytest.fixture(scope="module")
def stage(tmp_path_factory) -> Usd.Stage:
    out = tmp_path_factory.mktemp("usd") / "minimal.usda"
    convert(FIXTURE, out)
    return Usd.Stage.Open(str(out))


def _find(tree, predicate):
    for node in tree:
        if predicate(node):
            return node
        found = _find(node["children"], predicate)
        if found is not None:
            return found
    return None


def test_schema_top_level_keys(stage):
    result = build_scene_json(stage, assets={"gltf": "model.glb", "voxels": "voxels.json"})
    assert result["version"] == 1
    assert result["upAxis"] == "Z"
    assert result["assets"] == {"gltf": "model.glb", "voxels": "voxels.json"}
    assert isinstance(result["tree"], list)


def test_tree_root_is_site_not_ifc_model(stage):
    """spec.md の例と同様、tree の最上位は /IFC_Model 自体ではなく /IFC_Model/Site。"""
    result = build_scene_json(stage)
    assert len(result["tree"]) == 1
    assert result["tree"][0]["path"] == "/IFC_Model/Site"
    assert result["tree"][0]["class"] == "IfcSite"


def test_hierarchy_site_building_storey_element(stage):
    result = build_scene_json(stage)
    site = result["tree"][0]
    assert len(site["children"]) == 1
    building = site["children"][0]
    assert building["class"] == "IfcBuilding"

    assert len(building["children"]) == 1
    storey = building["children"][0]
    assert storey["class"] == "IfcBuildingStorey"
    assert storey["path"].startswith("/IFC_Model/Site/Building/Storey_")

    element_classes = {child["class"] for child in storey["children"]}
    assert element_classes == {"IfcWall"}
    assert len(storey["children"]) == 2  # 壁2枚


def test_node_has_guid_class_and_custom_data(stage):
    result = build_scene_json(stage)
    wall = _find(result["tree"], lambda n: n.get("name") == "Wall North")
    assert wall is not None
    assert wall["class"] == "IfcWall"
    assert wall["guid"]
    assert wall["customData"]["GUID"] == wall["guid"]
    assert wall["customData"]["class"] == "IfcWall"
    assert wall["customData"]["Name"] == "Wall North"


def test_mesh_child_prim_is_not_a_tree_node(stage):
    """meshは空間階層のノードではないため、tree/childrenに含まれない。"""
    result = build_scene_json(stage)
    wall = _find(result["tree"], lambda n: n.get("name") == "Wall North")
    child_paths = [c["path"] for c in wall["children"]]
    assert not any(p.endswith("/mesh") for p in child_paths)
    assert wall["children"] == []


def test_result_is_json_serializable(stage):
    result = build_scene_json(stage, assets={"gltf": "m.glb"})
    serialized = json.dumps(result, ensure_ascii=False)
    reloaded = json.loads(serialized)
    assert reloaded["tree"][0]["path"] == "/IFC_Model/Site"


def test_default_assets_is_empty_dict_when_omitted(stage):
    result = build_scene_json(stage)
    assert result["assets"] == {}


def test_rejects_stage_without_default_prim():
    """defaultPrim未設定のUSDでは、生のRuntimeErrorではなく分かりやすいValueError。"""
    stage_without_default = Usd.Stage.CreateInMemory()

    with pytest.raises(ValueError):
        build_scene_json(stage_without_default)


def test_up_axis_reflects_stage_metadata(tmp_path):
    usda = tmp_path / "yup.usda"
    convert(FIXTURE, usda, y_up=True)
    stage_yup = Usd.Stage.Open(str(usda))

    result = build_scene_json(stage_yup)
    assert result["upAxis"] == "Y"
