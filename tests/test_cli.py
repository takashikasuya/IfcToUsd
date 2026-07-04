"""CLI サブコマンド構成の回帰テスト。

`ifc2usd/cli.py` は `convert` サブコマンドを持つ（`voxelize`/`export-gltf`/`serve`
は今後のIssueで追加予定）。サブコマンド省略時の旧来の呼び出し (`ifc2usd <ifc> ...`)
は `convert` として扱われる後方互換を維持する。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pxr import Usd, UsdGeom

from ifc2usd.cli import main

FIXTURE = Path(__file__).parent / "fixtures" / "minimal.ifc"


def _open_stage(path: Path) -> Usd.Stage:
    stage = Usd.Stage.Open(str(path))
    assert stage is not None
    return stage


def test_legacy_invocation_without_subcommand(tmp_path):
    out = tmp_path / "legacy.usda"
    exit_code = main([str(FIXTURE), "-o", str(out)])
    assert exit_code == 0
    assert out.is_file()


def test_explicit_convert_subcommand(tmp_path):
    out = tmp_path / "explicit.usda"
    exit_code = main(["convert", str(FIXTURE), "-o", str(out)])
    assert exit_code == 0
    assert out.is_file()


def test_legacy_and_explicit_produce_equivalent_output(tmp_path):
    legacy_out = tmp_path / "legacy.usda"
    explicit_out = tmp_path / "explicit.usda"
    main([str(FIXTURE), "-o", str(legacy_out)])
    main(["convert", str(FIXTURE), "-o", str(explicit_out)])

    legacy_stage = _open_stage(legacy_out)
    explicit_stage = _open_stage(explicit_out)

    legacy_meshes = [p.GetPath() for p in legacy_stage.Traverse() if p.IsA(UsdGeom.Mesh)]
    explicit_meshes = [p.GetPath() for p in explicit_stage.Traverse() if p.IsA(UsdGeom.Mesh)]
    assert legacy_meshes == explicit_meshes


def test_y_up_flag_works_without_subcommand(tmp_path):
    out = tmp_path / "yup.usda"
    main([str(FIXTURE), "-o", str(out), "--y-up"])
    stage = _open_stage(out)
    assert UsdGeom.GetStageUpAxis(stage) == UsdGeom.Tokens.y


def test_verbose_flag_accepted_in_both_forms(tmp_path):
    out1 = tmp_path / "v1.usda"
    out2 = tmp_path / "v2.usda"
    assert main([str(FIXTURE), "-o", str(out1), "-v"]) == 0
    assert main(["convert", str(FIXTURE), "-o", str(out2), "--verbose"]) == 0


def test_missing_ifc_file_errors(tmp_path):
    missing = tmp_path / "does_not_exist.ifc"
    with pytest.raises(SystemExit) as excinfo:
        main([str(missing)])
    assert excinfo.value.code != 0


def test_empty_argv_errors():
    with pytest.raises(SystemExit) as excinfo:
        main([])
    assert excinfo.value.code != 0


def test_legacy_invocation_with_ifc_path_named_like_a_subcommand(monkeypatch, tmp_path):
    """パスがサブコマンド名と同名でも、実在するファイルなら旧来呼び出しとして扱う。"""
    monkeypatch.chdir(tmp_path)
    ifc_named_convert = tmp_path / "convert"
    ifc_named_convert.write_bytes(FIXTURE.read_bytes())

    out = tmp_path / "out.usda"
    exit_code = main(["convert", "-o", str(out)])
    assert exit_code == 0
    assert out.is_file()


def test_default_output_path_without_subcommand(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    exit_code = main([str(FIXTURE.resolve())])
    assert exit_code == 0
    assert (tmp_path / "output" / "minimal_structured.usda").is_file()
