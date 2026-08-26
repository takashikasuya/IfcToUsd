"""出力 USD の仕様適合検証（E10-10 / Issue #67）。

`UsdUtilsComplianceChecker` は削除済みで、検証は UsdValidation フレームワークへ
移行した。変換結果が登録済みバリデータ全てを通ることを回帰テストとして固定する。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pxr import Usd, UsdValidation

from ifc2usd import convert

FIXTURE = Path(__file__).parent / "fixtures" / "minimal.ifc"


def _validate(stage) -> list[str]:
    registry = UsdValidation.ValidationRegistry()
    context = UsdValidation.ValidationContext(registry.GetOrLoadAllValidators())
    return [f"{e.GetName()}: {e.GetMessage()}" for e in context.Validate(stage)]


@pytest.fixture(scope="module")
def stages(tmp_path_factory):
    out = tmp_path_factory.mktemp("usd")
    z_path, y_path = out / "z.usda", out / "y.usda"
    convert(FIXTURE, z_path)
    convert(FIXTURE, y_path, y_up=True)
    return Usd.Stage.Open(str(z_path)), Usd.Stage.Open(str(y_path))


def test_z_up_output_passes_usd_validation(stages):
    assert _validate(stages[0]) == []


def test_y_up_output_passes_usd_validation(stages):
    assert _validate(stages[1]) == []
