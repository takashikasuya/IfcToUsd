"""ボクセル化の重複計算の解消（E10-6 / Issue #63）の検証。

`voxelize` は JSON と PointInstancer の 2 形式を書き出すが、どちらも同じ
`(要素, サイズ)` の組をボクセル化していた。共有キャッシュ経由で 1 回に抑える。
"""

from __future__ import annotations

import ifc2usd.voxel as voxel_module
from ifc2usd.voxel import VoxelElement, build_voxel_json, build_voxel_stage

ELEMENTS = [
    VoxelElement(
        guid="A",
        cls="IfcWall",
        name="Wall",
        color=(1.0, 0.0, 0.0),
        vertices=[(0.0, 0.0, 0.0), (2.0, 0.0, 0.0), (2.0, 2.0, 0.0), (0.0, 2.0, 0.0)],
        indices=[0, 1, 2, 0, 2, 3],
    )
]
SIZES = [1.0, 0.5]


def _counting_voxelize(monkeypatch):
    calls: list[tuple] = []
    original = voxel_module.voxelize_mesh

    def spy(vertices, indices, size, **kwargs):
        calls.append((len(vertices), size))
        return original(vertices, indices, size, **kwargs)

    monkeypatch.setattr(voxel_module, "voxelize_mesh", spy)
    return calls


def test_shared_cache_voxelizes_each_element_and_size_once(monkeypatch, tmp_path):
    calls = _counting_voxelize(monkeypatch)

    cache: dict = {}
    build_voxel_json(ELEMENTS, sizes=SIZES, cache=cache)
    build_voxel_stage(
        ELEMENTS,
        sizes=SIZES,
        output_path=tmp_path / "voxels.usda",
        reference_asset_path="./model.usda",
        cache=cache,
    )

    assert len(calls) == len(ELEMENTS) * len(SIZES)


def test_results_match_the_uncached_path():
    cached = build_voxel_json(ELEMENTS, sizes=SIZES, cache={})
    plain = build_voxel_json(ELEMENTS, sizes=SIZES)
    assert cached == plain


def test_shared_cache_separates_fill_modes(monkeypatch):
    calls = _counting_voxelize(monkeypatch)
    cache: dict = {}

    build_voxel_json(ELEMENTS, sizes=[1.0], fill=False, cache=cache)
    build_voxel_json(ELEMENTS, sizes=[1.0], fill=True, cache=cache)

    assert len(calls) == 2
