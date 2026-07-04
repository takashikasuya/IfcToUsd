"""IFC → USD 変換の CLI エントリポイント。

例:
    uv run ifc2usd files/ToyodaLab.ifc
    uv run ifc2usd files/ToyodaLab.ifc -o output/model.usda --y-up --verbose
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ifc2usd", description="Convert an IFC model into a structured OpenUSD stage."
    )
    parser.add_argument("ifc_path", type=Path, help="Path to the input .ifc file")
    parser.add_argument(
        "-o", "--output", type=Path, default=None,
        help="Output .usd/.usda path (default: output/<name>_structured.usda)",
    )
    parser.add_argument("--y-up", action="store_true", help="Use Y-UP axis instead of the IFC default Z-UP")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if not args.ifc_path.is_file():
        parser.error(f"IFC file not found: {args.ifc_path}")

    output_path = args.output or _default_output(args.ifc_path)
    convert(args.ifc_path, output_path, y_up=args.y_up)
    return 0


if __name__ == "__main__":
    sys.exit(main())
