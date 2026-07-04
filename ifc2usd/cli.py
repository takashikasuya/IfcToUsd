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

# 既知のサブコマンド名。先頭引数がこれに一致しない場合は "convert" を補って後方互換を保つ。
_SUBCOMMANDS = ("convert",)


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
    if not args.ifc_path.is_file():
        parser.error(f"IFC file not found: {args.ifc_path}")

    output_path = args.output or _default_output(args.ifc_path)
    convert(args.ifc_path, output_path, y_up=args.y_up)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ifc2usd", description="Convert an IFC model into a structured OpenUSD stage."
    )
    subparsers = parser.add_subparsers(dest="command")

    convert_parser = subparsers.add_parser(
        "convert", help="Convert an IFC model into a structured OpenUSD stage (default command)."
    )
    _add_convert_arguments(convert_parser)
    convert_parser.set_defaults(_run=_run_convert, _parser=convert_parser)

    return parser


def _normalize_argv(argv: list[str]) -> list[str]:
    """サブコマンド省略時（旧来の呼び出し）に "convert" を補う。"""
    if not argv:
        return argv
    if argv[0] in _SUBCOMMANDS or argv[0] in ("-h", "--help"):
        return argv
    return ["convert", *argv]


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    argv = _normalize_argv(argv)

    parser = _build_parser()
    args = parser.parse_args(argv)

    if getattr(args, "command", None) is None:
        parser.error("a command is required (e.g. 'convert')")

    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    return args._run(args, args._parser)


if __name__ == "__main__":
    sys.exit(main())
