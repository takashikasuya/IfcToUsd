"""`ifc2usd voxelize` サブコマンドのE2Eテスト。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pxr import Usd

from ifc2usd import convert
from ifc2usd.cli import main

FIXTURE = Path(__file__).parent / "fixtures" / "minimal.ifc"


def test_voxelize_from_usda(tmp_path):
    usda = tmp_path / "minimal.usda"
    convert(FIXTURE, usda)

    out_base = tmp_path / "voxels"
    exit_code = main(["voxelize", str(usda), "--size", "0.5", "-o", str(out_base)])
    assert exit_code == 0

    json_path = out_base.with_suffix(".json")
    assert json_path.is_file()
    data = json.loads(json_path.read_text())
    assert data["version"] == 3
    assert len(data["lods"]) == 1
    assert data["lods"][0]["size"] == 0.5
    guids = {el["guid"] for el in data["lods"][0]["elements"]}
    assert len(guids) == 2  # 壁2枚


def test_voxelize_also_writes_pointinstancer_usda(tmp_path):
    """docs/viewer/spec.md §1.1: voxelizeは<base>.jsonに加え<base>.usda
    （PointInstancerレイヤー、§3）も出力する。"""
    from pxr import UsdGeom

    usda = tmp_path / "minimal.usda"
    convert(FIXTURE, usda)

    out_base = tmp_path / "voxels"
    exit_code = main(["voxelize", str(usda), "--size", "0.5", "--size", "0.25", "-o", str(out_base)])
    assert exit_code == 0

    voxel_usda_path = out_base.with_suffix(".usda")
    assert voxel_usda_path.is_file()

    stage = Usd.Stage.Open(str(voxel_usda_path))
    # referenceが解決され、正本(minimal.usda)の階層が見える。
    assert stage.GetPrimAtPath("/IFC_Model/Site").IsValid()

    instancer = UsdGeom.PointInstancer(stage.GetPrimAtPath("/IFC_Model/Voxels"))
    assert instancer.GetPrim().IsValid()
    variant_set = instancer.GetPrim().GetVariantSets().GetVariantSet("voxelLOD")
    assert set(variant_set.GetVariantNames()) == {"size_0_5", "size_0_25"}


def test_voxelize_from_ifc_directly_also_writes_pointinstancer_usda(tmp_path):
    """IFC直接入力でも、変換した正本USDへのreferenceを持つ.usdaが書ける
    （正本を一時ディレクトリに置くとreferenceが壊れるため、永続化して確認する）。"""
    from pxr import UsdGeom

    out_base = tmp_path / "voxels"
    exit_code = main(["voxelize", str(FIXTURE), "-o", str(out_base)])
    assert exit_code == 0

    voxel_usda_path = out_base.with_suffix(".usda")
    assert voxel_usda_path.is_file()

    stage = Usd.Stage.Open(str(voxel_usda_path))
    assert stage.GetPrimAtPath("/IFC_Model/Site").IsValid()
    instancer = UsdGeom.PointInstancer(stage.GetPrimAtPath("/IFC_Model/Voxels"))
    assert instancer.GetPrim().IsValid()


def test_voxelize_multiple_sizes(tmp_path):
    usda = tmp_path / "minimal.usda"
    convert(FIXTURE, usda)

    out_base = tmp_path / "voxels"
    exit_code = main(["voxelize", str(usda), "--size", "0.5", "--size", "0.25", "-o", str(out_base)])
    assert exit_code == 0

    data = json.loads(out_base.with_suffix(".json").read_text())
    assert [lod["size"] for lod in data["lods"]] == [0.5, 0.25]


def test_voxelize_default_size_is_half_meter(tmp_path):
    usda = tmp_path / "minimal.usda"
    convert(FIXTURE, usda)

    out_base = tmp_path / "voxels"
    main(["voxelize", str(usda), "-o", str(out_base)])
    data = json.loads(out_base.with_suffix(".json").read_text())
    assert [lod["size"] for lod in data["lods"]] == [0.5]


def test_voxelize_fill_flag(tmp_path):
    usda = tmp_path / "minimal.usda"
    convert(FIXTURE, usda)

    out_base = tmp_path / "voxels"
    exit_code = main(["voxelize", str(usda), "--fill", "-o", str(out_base)])
    assert exit_code == 0
    assert out_base.with_suffix(".json").is_file()


def test_voxelize_from_ifc_directly(tmp_path):
    out_base = tmp_path / "voxels"
    exit_code = main(["voxelize", str(FIXTURE), "-o", str(out_base)])
    assert exit_code == 0

    data = json.loads(out_base.with_suffix(".json").read_text())
    assert len(data["lods"][0]["elements"]) == 2


def test_voxelize_default_output_path(monkeypatch, tmp_path):
    usda = tmp_path / "minimal.usda"
    convert(FIXTURE, usda)
    monkeypatch.chdir(tmp_path)

    exit_code = main(["voxelize", str(usda)])
    assert exit_code == 0
    assert (tmp_path / "output" / "minimal_voxels.json").is_file()


def test_voxelize_rejects_missing_file(tmp_path):
    with pytest.raises(SystemExit):
        main(["voxelize", str(tmp_path / "does_not_exist.usda")])


def test_voxelize_rejects_unsupported_extension(tmp_path):
    bogus = tmp_path / "model.step"
    bogus.write_text("not a real file")
    with pytest.raises(SystemExit):
        main(["voxelize", str(bogus)])


def test_voxelize_rejects_stage_with_no_elements(tmp_path):
    """要素（GUID+class customData付きmesh）が1つもないUSDでは、
    生のValueErrorではなく通常のCLIエラー（SystemExit）になる。"""
    from pxr import Usd, UsdGeom

    empty_usda = tmp_path / "empty.usda"
    stage = Usd.Stage.CreateNew(str(empty_usda))
    UsdGeom.Xform.Define(stage, "/Empty")
    stage.GetRootLayer().Save()

    with pytest.raises(SystemExit):
        main(["voxelize", str(empty_usda)])


def test_voxelize_source_metadata_records_input_filename(tmp_path):
    usda = tmp_path / "minimal.usda"
    convert(FIXTURE, usda)

    out_base = tmp_path / "voxels"
    main(["voxelize", str(usda), "-o", str(out_base)])
    data = json.loads(out_base.with_suffix(".json").read_text())
    assert data["source"]["usd"] == "minimal.usda"
