"""ビューワーの純関数ユニットテスト（E10-9 / Issue #66）を pytest から実行する。

`viewer.js` は three.js と DOM に強く結合していて Playwright でしか動かせないが、
Morton デコードや turbo カラーマップのような純関数は `viewer/lib/` の ES モジュール
へ切り出してある。`node --test` で走らせ、検証経路を pytest 1 本に保つ。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

JS_TEST_DIR = Path(__file__).parent / "viewer"

# Node は PATH に無くても既定の場所にあることが多い（この環境がまさにそう）
_FALLBACK_NODE = Path(r"C:\Program Files\nodejs\node.exe")


def _node_executable() -> str | None:
    found = shutil.which("node")
    if found:
        return found
    return str(_FALLBACK_NODE) if _FALLBACK_NODE.exists() else None


def test_viewer_pure_function_units():
    node = _node_executable()
    if node is None:
        pytest.skip("node が見つからないため viewer の JS ユニットテストをスキップ")

    # ディレクトリ指定は Node のバージョンによって解釈が変わるのでファイルを直接渡す
    test_files = sorted(str(p) for p in JS_TEST_DIR.glob("*.test.js"))
    assert test_files, f"no JS unit tests found in {JS_TEST_DIR}"

    result = subprocess.run(
        [node, "--test", *test_files],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, result.stdout + result.stderr
