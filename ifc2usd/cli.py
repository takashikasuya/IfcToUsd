"""IFC → USD 変換の CLI エントリポイント。

サブコマンド構成: ``convert``（他のサブコマンドは今後追加予定）。
後方互換のため、サブコマンド名を省略した旧来の呼び出し
(``ifc2usd <ifc> ...``) は ``convert`` として扱う。

例:
    uv run ifc2usd files/ToyodaLab.ifc
    uv run ifc2usd convert files/ToyodaLab.ifc -o output/model.usda --y-up --verbose
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import ifcopenshell
from tqdm import tqdm

from .ifc import create_settings, get_geometry
from .usd import build_stage

logger = logging.getLogger("ifc2usd")


def convert(ifc_path: Path, output_path: Path, y_up: bool = False) -> Path:
    """IFC ファイルを構造化された USD ステージへ変換して書き出す。"""
    logger.info("Opening IFC: %s", ifc_path)
    ifc_file = ifcopenshell.open(str(ifc_path))

    settings = create_settings()

    geometries: dict = {}
    materials: dict = {}
    for verts, indices, norms, info, material, color, translate in tqdm(
        get_geometry(settings, ifc_file, materials, y_up=y_up), desc="Reading geometry"
    ):
        faces = [3] * (len(indices) // 3)
        geometries[info["GlobalId"]] = [faces, verts, indices, material, color, norms, translate]

    logger.info("Extracted %d geometries, %d materials", len(geometries), len(materials))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    build_stage(ifc_file, geometries, materials, str(output_path), y_up=y_up)
    logger.info("Wrote USD: %s", output_path)
    return output_path


def _default_output(ifc_path: Path) -> Path:
    return Path("output") / f"{ifc_path.stem}_structured.usda"


def _add_convert_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("ifc_path", type=Path, help="Path to the input .ifc file")
    parser.add_argument(
        "-o", "--output", type=Path, default=None,
        help="Output .usd/.usda path (default: output/<name>_structured.usda)",
    )
    parser.add_argument("--y-up", action="store_true", help="Use Y-UP axis instead of the IFC default Z-UP")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging")


def _run_convert(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if not args.ifc_path.is_file():
        parser.error(f"IFC file not found: {args.ifc_path}")

    output_path = args.output or _default_output(args.ifc_path)
    convert(args.ifc_path, output_path, y_up=args.y_up)
    return 0


# サブコマンド名 -> (引数登録関数, 実行関数, help文字列)。
# _build_parser と _normalize_argv の両方がここを唯一の正本として参照するため、
# 新しいサブコマンド（voxelize/export-gltf/serve）を追加する際はここに1エントリ
# 加えるだけでよく、登録漏れによる `args._run` の AttributeError や
# `_normalize_argv` の判定漏れが構造的に起きない。
_COMMANDS: dict[str, tuple] = {
    "convert": (
        _add_convert_arguments,
        _run_convert,
        "Convert an IFC model into a structured OpenUSD stage (default command).",
    ),
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ifc2usd", description="Convert an IFC model into a structured OpenUSD stage."
    )
    subparsers = parser.add_subparsers(dest="command")

    for name, (add_arguments, run, help_text) in _COMMANDS.items():
        subparser = subparsers.add_parser(name, help=help_text)
        add_arguments(subparser)
        subparser.set_defaults(_run=run, _parser=subparser)

    return parser


def _normalize_argv(argv: list[str]) -> list[str]:
    """サブコマンド省略時（旧来の呼び出し）に "convert" を補う。

    既知の制限: 先頭引数がサブコマンド名（例 "convert"）と完全に一致する場合は
    サブコマンド呼び出しとして扱う。ただし、その名前と同名のファイルが実在する
    場合（例: 拡張子なしで "convert" という名前の IFC ファイル）は、パスとして
    扱う旧来呼び出しを優先する。
    """
    if not argv:
        return argv
    first = argv[0]
    if first in ("-h", "--help"):
        return argv
    if first in _COMMANDS and not Path(first).is_file():
        return argv
    return ["convert", *argv]


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    argv = _normalize_argv(argv)

    parser = _build_parser()
    args = parser.parse_args(argv)

    if getattr(args, "command", None) is None:
        parser.error("a command is required (e.g. 'convert')")

    return args._run(args, args._parser)


if __name__ == "__main__":
    sys.exit(main())
