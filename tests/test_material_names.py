"""マテリアル名 → USD prim 名の変換（E10-2 / Issue #59）のユニットテスト。

`sanitize_material_name` は IFC 由来の任意の文字列を USD の prim 名として
成立する識別子へ変換する。usd-core 26.5 では prim 名は UTF-8 を受け付けるため、
日本語名は潰さずに保持することも要件に含む。
"""

from __future__ import annotations

import pytest
from pxr import Sdf, Usd, UsdShade

from ifc2usd.ifc import MaterialNameRegistry, sanitize_material_name

# 変換前の名前。IFC には空白・記号・先頭数字・日本語が実際に現れる
ADVERSARIAL_NAMES = [
    "Concrete 1",
    "Concrete-1",
    "Concrete(1)",
    "Concrete,1",
    "2nd Layer",
    "<Unnamed>",
    "a/b",
    "  ",
    "",
    "壁材",
    "コンクリート 打放し",
]


@pytest.mark.parametrize("name", ADVERSARIAL_NAMES)
def test_sanitized_name_is_a_valid_prim_name(name):
    assert Sdf.Path.IsValidIdentifier(sanitize_material_name(name))


@pytest.mark.parametrize("name", ADVERSARIAL_NAMES)
def test_sanitized_name_can_define_a_material_prim(name):
    stage = Usd.Stage.CreateInMemory()
    path = Sdf.Path(f"/Materials/{sanitize_material_name(name)}")
    assert UsdShade.Material.Define(stage, path)


def test_utf8_name_is_preserved_not_collapsed():
    # Tf.MakeValidIdentifier は日本語を '_' の羅列へ潰してしまうため使わない
    assert sanitize_material_name("壁材") == "壁材"


def test_registry_disambiguates_colliding_names():
    registry = MaterialNameRegistry()
    first = registry.resolve("Concrete 1")
    second = registry.resolve("Concrete-1")
    assert first != second
    assert Sdf.Path.IsValidIdentifier(second)


def test_registry_is_stable_for_the_same_name():
    registry = MaterialNameRegistry()
    assert registry.resolve("Concrete 1") == registry.resolve("Concrete 1")
