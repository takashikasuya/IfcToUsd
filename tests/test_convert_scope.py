"""`convert` の範囲指定（`--storey` / `--global-ids`）のテスト。

`generate_fixture.build()` の 1 階（壁 2 枚）に、2 階を足したモデルを作って確かめる。2 階には
- 直接置いた壁（Wall Upper）
- 2 階に分解された IfcSpace の中に置いた壁（Wall In Space）
- 形状を持たない IfcElementAssembly と、その部品の壁（Part A・Part B。部品は空間に直接は置かない）
がある。
"""

from __future__ import annotations

import json
from pathlib import Path

import ifcopenshell
import ifcopenshell.api.aggregate
import ifcopenshell.api.root
import ifcopenshell.api.spatial
import ifcopenshell.util.shape_builder
import pytest
from pxr import Usd, UsdGeom

from ifc2usd import ConvertScope, convert
from ifc2usd.cli import main
from tests.generate_fixture import _add_wall, build


def _build_two_storeys() -> ifcopenshell.file:
    model = build()
    building = model.by_type("IfcBuilding")[0]
    body = next(c for c in model.by_type("IfcGeometricRepresentationSubContext") if c.ContextIdentifier == "Body")
    builder = ifcopenshell.util.shape_builder.ShapeBuilder(model)

    upper = ifcopenshell.api.root.create_entity(model, ifc_class="IfcBuildingStorey", name="Upper Floor")
    ifcopenshell.api.aggregate.assign_object(model, products=[upper], relating_object=building)
    _add_wall(model, body, upper, builder, "Wall Upper", (0.0, 0.0, 3.0), (5.0, 0.2, 3.0), (0.1, 0.8, 0.1))

    space = ifcopenshell.api.root.create_entity(model, ifc_class="IfcSpace", name="Room")
    ifcopenshell.api.aggregate.assign_object(model, products=[space], relating_object=upper)
    _add_wall(model, body, space, builder, "Wall In Space", (0.0, 2.0, 3.0), (2.0, 0.2, 3.0), (0.5, 0.5, 0.1))

    assembly = ifcopenshell.api.root.create_entity(model, ifc_class="IfcElementAssembly", name="Assembly")
    ifcopenshell.api.spatial.assign_container(model, products=[assembly], relating_structure=upper)
    parts = []
    for name, origin in (("Part A", (3.0, 3.0, 3.0)), ("Part B", (4.0, 3.0, 3.0))):
        part = _add_wall(model, body, upper, builder, name, origin, (0.5, 0.5, 0.5), (0.3, 0.3, 0.3))
        ifcopenshell.api.spatial.unassign_container(model, products=[part])
        parts.append(part)
    ifcopenshell.api.aggregate.assign_object(model, products=parts, relating_object=assembly)
    return model


@pytest.fixture(scope="module")
def model_path(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("ifc") / "two_storeys.ifc"
    _build_two_storeys().write(str(path))
    return path


@pytest.fixture(scope="module")
def model(model_path) -> ifcopenshell.file:
    return ifcopenshell.open(str(model_path))


def _guid(model: ifcopenshell.file, name: str) -> str:
    return next(e.GlobalId for e in model.by_type("IfcRoot") if getattr(e, "Name", None) == name)


def _mesh_names(path: Path) -> set[str]:
    """メッシュを持つ prim（mesh の親）の Name を返す。"""
    stage = Usd.Stage.Open(str(path))
    names = set()
    for prim in stage.Traverse():
        if prim.IsA(UsdGeom.Mesh):
            names.add(prim.GetParent().GetCustomDataByKey("Name"))
    return names


def _placed_names(path: Path) -> set[str]:
    stage = Usd.Stage.Open(str(path))
    return {
        prim.GetCustomDataByKey("Name")
        for prim in stage.Traverse()
        if prim.GetName().startswith(("Element_", "Object_"))
    }


def test_without_scope_converts_everything(model_path, tmp_path):
    out = tmp_path / "all.usda"
    convert(model_path, out)
    assert _mesh_names(out) == {"Wall North", "Wall East", "Wall Upper", "Wall In Space", "Part A", "Part B"}
    stage = Usd.Stage.Open(str(out))
    assert stage.GetDefaultPrim().GetCustomDataByKey("ifcScope") is None


def test_storey_scope_keeps_only_that_storey_including_spaces_and_parts(model, model_path, tmp_path):
    out = tmp_path / "upper.usda"
    upper = _guid(model, "Upper Floor")
    convert(model_path, out, scope=ConvertScope(storey=upper))

    assert _mesh_names(out) == {"Wall Upper", "Wall In Space", "Part A", "Part B"}
    # 範囲外の要素は prim ごと出さない（空の Xform も残さない）
    assert not {"Wall North", "Wall East"} & _placed_names(out)
    stage = Usd.Stage.Open(str(out))
    # 空間の階層（Site・Building・両方の Storey）は保つ
    storeys = {p.GetCustomDataByKey("Name") for p in stage.Traverse() if p.GetName().startswith("Storey_")}
    assert storeys == {"Ground Floor", "Upper Floor"}
    assert dict(stage.GetDefaultPrim().GetCustomDataByKey("ifcScope")) == {"storey": upper}


def test_global_ids_scope_keeps_listed_elements_and_their_parts(model, model_path, tmp_path):
    out = tmp_path / "ids.usda"
    ids = [_guid(model, "Wall East"), _guid(model, "Assembly")]
    convert(model_path, out, scope=ConvertScope(global_ids=tuple(ids)))

    assert _mesh_names(out) == {"Wall East", "Part A", "Part B"}
    assert _placed_names(out) == {"Wall East", "Assembly", "Part A", "Part B"}
    stage = Usd.Stage.Open(str(out))
    recorded = dict(stage.GetDefaultPrim().GetCustomDataByKey("ifcScope"))
    assert list(recorded["globalIds"]) == sorted(ids)


def test_global_ids_scope_with_only_a_part_keeps_its_parent_as_an_empty_group(model, model_path, tmp_path):
    out = tmp_path / "part.usda"
    convert(model_path, out, scope=ConvertScope(global_ids=(_guid(model, "Part A"),)))

    assert _mesh_names(out) == {"Part A"}
    # 部品だけを選んでも、親の集約は階層を保つための空のグループとして残る
    assert _placed_names(out) == {"Assembly", "Part A"}


def test_unknown_global_id_is_rejected(model_path, tmp_path):
    with pytest.raises(ValueError, match="not found"):
        convert(model_path, tmp_path / "x.usda", scope=ConvertScope(global_ids=("0000000000000000000000",)))


def test_storey_must_be_a_building_storey(model, model_path, tmp_path):
    with pytest.raises(ValueError, match="IfcBuildingStorey"):
        convert(model_path, tmp_path / "x.usda", scope=ConvertScope(storey=_guid(model, "Wall East")))


def test_scope_needs_exactly_one_of_storey_or_global_ids():
    with pytest.raises(ValueError):
        ConvertScope()
    with pytest.raises(ValueError):
        ConvertScope(storey="a", global_ids=("b",))


def test_cli_storey(model, model_path, tmp_path):
    out = tmp_path / "cli-storey.usda"
    assert main(["convert", str(model_path), "-o", str(out), "--storey", _guid(model, "Ground Floor")]) == 0
    assert _mesh_names(out) == {"Wall North", "Wall East"}


def test_cli_global_ids_file_ignores_blank_lines_and_comments(model, model_path, tmp_path):
    ids_file = tmp_path / "ids.txt"
    ids_file.write_text(f"# selected\n{_guid(model, 'Wall Upper')}\n\n  {_guid(model, 'Wall North')}  \n")
    out = tmp_path / "cli-ids.usda"
    assert main(["convert", str(model_path), "-o", str(out), "--global-ids", str(ids_file)]) == 0
    assert _mesh_names(out) == {"Wall Upper", "Wall North"}


def test_cli_rejects_both_options(model, model_path, tmp_path):
    ids_file = tmp_path / "ids.txt"
    ids_file.write_text(_guid(model, "Wall Upper") + "\n")
    with pytest.raises(SystemExit) as excinfo:
        main(["convert", str(model_path), "--storey", "x", "--global-ids", str(ids_file)])
    assert excinfo.value.code == 2


def test_cli_rejects_empty_ids_file_and_unknown_ids(model_path, tmp_path):
    empty = tmp_path / "empty.txt"
    empty.write_text("# nothing\n\n")
    with pytest.raises(SystemExit) as excinfo:
        main(["convert", str(model_path), "-o", str(tmp_path / "a.usda"), "--global-ids", str(empty)])
    assert excinfo.value.code == 2

    unknown = tmp_path / "unknown.txt"
    unknown.write_text("0000000000000000000000\n")
    with pytest.raises(SystemExit) as excinfo:
        main(["convert", str(model_path), "-o", str(tmp_path / "b.usda"), "--global-ids", str(unknown)])
    assert excinfo.value.code == 2


def test_scope_is_carried_into_the_gltf_export(model, model_path, tmp_path):
    usd = tmp_path / "upper.usdc"
    glb = tmp_path / "upper.glb"
    assert main(["convert", str(model_path), "-o", str(usd), "--storey", _guid(model, "Upper Floor")]) == 0
    assert main(["export-gltf", str(usd), "-o", str(glb)]) == 0
    # GLB の JSON チャンクから、メッシュを持つノードの名前を集める
    data = glb.read_bytes()
    length = int.from_bytes(data[12:16], "little")
    document = json.loads(data[20 : 20 + length])
    names = {n.get("extras", {}).get("name") for n in document["nodes"] if "mesh" in n}
    assert names == {"Wall Upper", "Wall In Space", "Part A", "Part B"}


def test_global_ids_scope_rejects_spatial_elements(model, model_path, tmp_path):
    with pytest.raises(ValueError, match="storey scope"):
        convert(model_path, tmp_path / "x.usda", scope=ConvertScope(global_ids=(_guid(model, "Upper Floor"),)))
