"""PointInstancer ボクセルレイヤーライター（`docs/viewer/spec.md` §3）のテスト。

正本 USD（`tests/fixtures/minimal.ifc` を変換したもの）への reference のみを持つ
独立ファイルを生成し、Stage.Open → variantSet 切替 → positions/protoIndices の
入れ替わりを検証する。正本自体は変更しないことも確認する。
"""

from __future__ import annotations

import filecmp
from pathlib import Path

import pytest
from pxr import Usd, UsdGeom

from ifc2usd import convert
from ifc2usd.usd import elements_from_stage
from ifc2usd.voxel import build_voxel_stage

FIXTURE = Path(__file__).parent / "fixtures" / "minimal.ifc"


@pytest.fixture
def workspace(tmp_path):
    """正本USDとボクセルレイヤーUSDを同じディレクトリに書き出す（reference解決のため）。"""
    structured = tmp_path / "minimal.usda"
    convert(FIXTURE, structured)

    stage = Usd.Stage.Open(str(structured))
    elements = elements_from_stage(stage)

    voxels_path = tmp_path / "minimal_voxels.usda"
    build_voxel_stage(
        elements, sizes=[0.5, 0.25], reference_asset_path="minimal.usda", output_path=str(voxels_path)
    )

    return tmp_path, structured, voxels_path, elements


def test_reference_resolves_and_does_not_modify_canonical_usd(workspace):
    tmp_path, structured, voxels_path, elements = workspace

    # 正本を独立に再変換した結果と、build_voxel_stage実行後の正本ファイルが一致する
    # （正本が一切変更されていないことを保証する）
    control = tmp_path / "control.usda"
    convert(FIXTURE, control)
    assert filecmp.cmp(structured, control, shallow=False)

    stage = Usd.Stage.Open(str(voxels_path))
    # reference が解決され、正本の階層（Site）が見える
    assert stage.GetPrimAtPath("/IFC_Model/Site").IsValid()
    assert stage.GetPrimAtPath("/IFC_Model/Site/Building").IsValid()


def test_point_instancer_purpose_is_proxy(workspace):
    _, _, voxels_path, _ = workspace
    stage = Usd.Stage.Open(str(voxels_path))
    instancer_prim = stage.GetPrimAtPath("/IFC_Model/Voxels")
    assert instancer_prim.IsA(UsdGeom.PointInstancer)
    assert UsdGeom.Imageable(instancer_prim).ComputePurpose() == UsdGeom.Tokens.proxy


def test_variant_switch_changes_positions_and_proto_indices(workspace):
    _, _, voxels_path, elements = workspace
    stage = Usd.Stage.Open(str(voxels_path))
    instancer = UsdGeom.PointInstancer(stage.GetPrimAtPath("/IFC_Model/Voxels"))
    variant_set = instancer.GetPrim().GetVariantSets().GetVariantSet("voxelLOD")

    assert set(variant_set.GetVariantNames()) == {"size_0_5", "size_0_25"}

    variant_set.SetVariantSelection("size_0_5")
    positions_05 = instancer.GetPositionsAttr().Get()
    proto_indices_05 = instancer.GetProtoIndicesAttr().Get()
    assert len(positions_05) == len(proto_indices_05)
    assert len(positions_05) == 60 + 48  # Wall North + Wall East @ 0.5m（既知の解析値）

    variant_set.SetVariantSelection("size_0_25")
    positions_025 = instancer.GetPositionsAttr().Get()
    assert len(positions_025) != len(positions_05)
    assert len(positions_025) > len(positions_05)  # より細かい格子ほどボクセル数は多い


def test_default_variant_selection_is_first_requested_size(workspace):
    _, _, voxels_path, _ = workspace
    stage = Usd.Stage.Open(str(voxels_path))
    instancer = UsdGeom.PointInstancer(stage.GetPrimAtPath("/IFC_Model/Voxels"))
    variant_set = instancer.GetPrim().GetVariantSets().GetVariantSet("voxelLOD")
    assert variant_set.GetVariantSelection() == "size_0_5"


def test_one_prototype_per_element_with_display_color(workspace):
    _, _, voxels_path, elements = workspace
    stage = Usd.Stage.Open(str(voxels_path))
    instancer = UsdGeom.PointInstancer(stage.GetPrimAtPath("/IFC_Model/Voxels"))
    variant_set = instancer.GetPrim().GetVariantSets().GetVariantSet("voxelLOD")
    variant_set.SetVariantSelection("size_0_5")

    targets = instancer.GetPrototypesRel().GetTargets()
    assert len(targets) == len(elements) == 2

    colors_by_guid = {el.guid: el.color for el in elements}
    ranges = instancer.GetPrim().GetCustomData()["elementRanges"]
    assert set(ranges.keys()) == {el.guid for el in elements}

    for guid, (start, count) in ranges.items():
        proto_indices = instancer.GetProtoIndicesAttr().Get()
        proto_index = proto_indices[start]
        # そのGUIDの全インスタンスが同じprototype(=同じ色)を指している
        assert all(proto_indices[start + i] == proto_index for i in range(count))
        cube = UsdGeom.Cube(stage.GetPrimAtPath(targets[proto_index]))
        expected_color = colors_by_guid[guid]
        actual_color = tuple(cube.GetDisplayColorAttr().Get()[0])
        assert tuple(round(c, 3) for c in actual_color) == tuple(round(c, 3) for c in expected_color)
        assert cube.GetSizeAttr().Get() == 0.5


def test_element_ranges_cover_all_positions_without_overlap(workspace):
    _, _, voxels_path, elements = workspace
    stage = Usd.Stage.Open(str(voxels_path))
    instancer = UsdGeom.PointInstancer(stage.GetPrimAtPath("/IFC_Model/Voxels"))
    variant_set = instancer.GetPrim().GetVariantSets().GetVariantSet("voxelLOD")
    variant_set.SetVariantSelection("size_0_5")

    ranges = instancer.GetPrim().GetCustomData()["elementRanges"]
    total = len(instancer.GetPositionsAttr().Get())

    covered = [False] * total
    for start, count in ranges.values():
        for i in range(start, start + count):
            assert not covered[i]
            covered[i] = True
    assert all(covered)
