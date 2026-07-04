"""`ifc2usd export-gltf` サブコマンドのE2Eテスト。"""

from __future__ import annotations

from pathlib import Path

import pytest
import trimesh

from ifc2usd import convert
from ifc2usd.cli import main

FIXTURE = Path(__file__).parent / "fixtures" / "minimal.ifc"


def test_export_gltf_from_usda(tmp_path):
    usda = tmp_path / "minimal.usda"
    convert(FIXTURE, usda)

    out = tmp_path / "minimal.glb"
    exit_code = main(["export-gltf", str(usda), "-o", str(out)])
    assert exit_code == 0
    assert out.is_file()

    scene = trimesh.load(str(out))
    assert len(scene.geometry) == 2  # 壁2枚


def test_export_gltf_default_output_path(monkeypatch, tmp_path):
    usda = tmp_path / "minimal.usda"
    convert(FIXTURE, usda)
    monkeypatch.chdir(tmp_path)

    exit_code = main(["export-gltf", str(usda)])
    assert exit_code == 0
    assert (tmp_path / "output" / "minimal.glb").is_file()


def test_export_gltf_rejects_missing_file(tmp_path):
    with pytest.raises(SystemExit):
        main(["export-gltf", str(tmp_path / "does_not_exist.usda")])
