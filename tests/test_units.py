"""単位系の扱い（E10-4 / Issue #61）の検証。

IfcOpenShell はジオメトリをファイルの長さ単位（mm など）ではなく常にメートルへ
正規化して返すため、ステージの ``metersPerUnit`` は 1.0 が正しい。ここではその
前提が非メートル単位の IFC でも崩れていないことを回帰テストとして固定する。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pxr import Usd, UsdGeom

from ifc2usd import convert
from ifc2usd.ifc import get_length_unit_scale

sys.path.insert(0, str(Path(__file__).parent))
import generate_fixture  # noqa: E402

import ifcopenshell  # noqa: E402


@pytest.fixture(scope="module")
def millimetre_ifc(tmp_path_factory) -> Path:
    """寸法をミリメートルで記述した、内容としては minimal.ifc と等価な IFC。"""
    path = tmp_path_factory.mktemp("ifc") / "minimal_mm.ifc"
    generate_fixture.build(millimetres=True).write(str(path))
    return path


def test_get_length_unit_scale_reads_millimetres(millimetre_ifc):
    assert get_length_unit_scale(ifcopenshell.open(str(millimetre_ifc))) == pytest.approx(0.001)


def test_get_length_unit_scale_reads_metres():
    fixture = Path(__file__).parent / "fixtures" / "minimal.ifc"
    assert get_length_unit_scale(ifcopenshell.open(str(fixture))) == pytest.approx(1.0)


@pytest.fixture(scope="module")
def millimetre_stage(millimetre_ifc, tmp_path_factory) -> Usd.Stage:
    out = tmp_path_factory.mktemp("usd") / "minimal_mm.usda"
    convert(millimetre_ifc, out)
    return Usd.Stage.Open(str(out))


def test_millimetre_model_is_still_metre_scale(millimetre_stage):
    assert UsdGeom.GetStageMetersPerUnit(millimetre_stage) == 1.0

    bbox = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_])
    aligned = bbox.ComputeWorldBound(millimetre_stage.GetDefaultPrim()).ComputeAlignedRange()
    assert tuple(round(v, 3) for v in aligned.GetMax()) == (5.2, 4.0, 3.0)


def test_source_length_unit_is_recorded_on_the_root_prim(millimetre_stage):
    root = millimetre_stage.GetDefaultPrim()
    assert root.GetCustomDataByKey("ifcLengthUnitScale") == pytest.approx(0.001)
