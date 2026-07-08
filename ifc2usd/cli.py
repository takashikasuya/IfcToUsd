"""IFC → USD 変換の CLI エントリポイント。

サブコマンド構成: ``convert`` / ``voxelize`` / ``export-gltf`` / ``serve``。
後方互換のため、サブコマンド名を省略した旧来の呼び出し
(``ifc2usd <ifc> ...``) は ``convert`` として扱う。

例:
    uv run ifc2usd files/ToyodaLab.ifc
    uv run ifc2usd convert files/ToyodaLab.ifc -o output/model.usda --y-up --verbose
    uv run ifc2usd voxelize output/model.usda --size 0.5
    uv run ifc2usd export-gltf output/model.usda
    uv run ifc2usd serve output/model.usda
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
import webbrowser
from pathlib import Path

import ifcopenshell
from pxr import Usd, UsdGeom
from tqdm import tqdm

from . import __version__
from .gltf import export_gltf
from .ifc import create_settings, get_geometry
from .mapping import MappingValidationError
from .serve import build_serve_directory, make_server
from .twin import TwinClient, build_twin_json
from .twin_proxy import TwinProxy, load_twin_config
from .usd import build_stage, elements_from_stage
from .voxel import build_voxel_json, build_voxel_stage

logger = logging.getLogger("ifc2usd")

_USD_EXTENSIONS = (".usd", ".usda", ".usdc")


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


def _configure_logging(verbose: bool) -> None:
    # force=True: 同一プロセス内で複数のサブコマンドが呼ばれても
    # （テストスイートなど）毎回のverbose設定を確実に反映させる。
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        force=True,
    )


def _run_convert(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    _configure_logging(args.verbose)
    if not args.ifc_path.is_file():
        parser.error(f"IFC file not found: {args.ifc_path}")

    output_path = args.output or _default_output(args.ifc_path)
    convert(args.ifc_path, output_path, y_up=args.y_up)
    return 0


def _default_voxel_output(input_path: Path) -> Path:
    return Path("output") / f"{input_path.stem}_voxels"


def _add_voxelize_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "input_path", type=Path, help="Path to a converted .usda/.usd (recommended) or a source .ifc file"
    )
    parser.add_argument(
        "--size", type=float, action="append", dest="sizes",
        help="Voxel size in meters (repeatable for multiple LODs; default: 0.5)",
    )
    parser.add_argument(
        "--fill", action="store_true", help="Include interior fill in addition to surface occupancy"
    )
    parser.add_argument(
        "-o", "--output", type=Path, default=None,
        help="Output base path without extension (default: output/<name>_voxels)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging")


def _run_voxelize(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    _configure_logging(args.verbose)
    if not args.input_path.is_file():
        parser.error(f"input file not found: {args.input_path}")

    sizes = args.sizes or [0.5]
    suffix = args.input_path.suffix.lower()

    output_base = args.output or _default_voxel_output(args.input_path)
    output_base.parent.mkdir(parents=True, exist_ok=True)

    if suffix in _USD_EXTENSIONS:
        reference_path = args.input_path
        stage = Usd.Stage.Open(str(reference_path))
        elements = elements_from_stage(stage)
    elif suffix == ".ifc":
        # PointInstancerレイヤー（.usda）は正本USDへの相対referenceを持つため、
        # 変換元USDはtempディレクトリではなく出力先の隣に永続化する
        # （tempに置くとreferenceが壊れたリンクになってしまう）。
        reference_path = output_base.parent / f"{args.input_path.stem}_structured.usda"
        convert(args.input_path, reference_path)
        stage = Usd.Stage.Open(str(reference_path))
        elements = elements_from_stage(stage)
    else:
        parser.error(f"unsupported input file type: {suffix or args.input_path}")

    if not elements:
        parser.error(f"no voxelizable elements found in: {args.input_path}")

    up_axis = str(UsdGeom.GetStageUpAxis(stage))
    source_name = reference_path.name

    result = build_voxel_json(
        elements,
        sizes=sizes,
        source={"usd": source_name, "generator": f"ifc2usd {__version__}"},
        up_axis=up_axis,
        fill=args.fill,
    )
    json_path = output_base.with_suffix(".json")
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    logger.info("Wrote voxel JSON: %s", json_path)

    usda_path = output_base.with_suffix(".usda")
    reference_asset_path = os.path.relpath(reference_path, start=usda_path.parent)
    build_voxel_stage(
        elements,
        sizes=sizes,
        reference_asset_path=reference_asset_path,
        output_path=str(usda_path),
        up_axis=up_axis,
        fill=args.fill,
    )
    logger.info("Wrote voxel PointInstancer layer: %s", usda_path)
    return 0


def _default_gltf_output(input_path: Path) -> Path:
    return Path("output") / f"{input_path.stem}.glb"


def _add_export_gltf_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("usd_path", type=Path, help="Path to a converted .usda/.usd/.usdc file")
    parser.add_argument(
        "-o", "--output", type=Path, default=None,
        help="Output .glb path (default: output/<name>.glb)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging")


def _run_export_gltf(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    _configure_logging(args.verbose)
    if not args.usd_path.is_file():
        parser.error(f"USD file not found: {args.usd_path}")

    stage = Usd.Stage.Open(str(args.usd_path))
    output_path = args.output or _default_gltf_output(args.usd_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    export_gltf(stage, str(output_path))
    logger.info("Wrote GLB: %s", output_path)
    return 0


def _add_serve_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("usd_path", type=Path, help="Path to a converted .usda/.usd/.usdc file")
    parser.add_argument("--port", type=int, default=8000, help="Port to listen on (default: 8000)")
    parser.add_argument(
        "--no-open", action="store_true", help="Do not open a browser automatically"
    )
    parser.add_argument(
        "--sdf-slices", action="store_true",
        help="Also compute per-element narrow-band SDF horizontal slices (E5-3) for viewer overlay",
    )
    parser.add_argument(
        "--twin", type=Path, default=None,
        help=(
            "Path to a twin-config.json (Building OS connection + metrics + mapping.json "
            "path) to enable live digital-twin mode (E9-3)"
        ),
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging")


def _run_serve(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    _configure_logging(args.verbose)
    if not args.usd_path.is_file():
        parser.error(f"USD file not found: {args.usd_path}")

    twin_json = None
    twin_proxy = None
    if args.twin:
        if not args.twin.is_file():
            parser.error(f"twin config file not found: {args.twin}")
        try:
            twin_config = load_twin_config(args.twin)
        except (json.JSONDecodeError, KeyError, FileNotFoundError, MappingValidationError) as exc:
            parser.error(f"invalid twin config {args.twin}: {exc}")
        twin_json = build_twin_json(
            twin_config["metrics"],
            twin_config["bindings"],
            poll_interval_seconds=twin_config["poll_interval_seconds"],
            stale_threshold_seconds=twin_config["stale_threshold_seconds"],
        )
        client = TwinClient(twin_config["base_url"], token=twin_config["token"])
        twin_proxy = TwinProxy(
            client, twin_config["bindings"], ttl_seconds=twin_config["poll_interval_seconds"]
        )

    with tempfile.TemporaryDirectory(prefix="ifc2usd_serve_") as tmpdir:
        workdir = Path(tmpdir)
        build_serve_directory(args.usd_path, workdir, sdf_slices=args.sdf_slices, twin=twin_json)

        try:
            server = make_server(workdir, port=args.port, twin_proxy=twin_proxy)
        except OSError as exc:
            parser.error(f"could not listen on port {args.port}: {exc}")

        host, port = server.server_address[:2]
        url = f"http://{host}:{port}/"
        logger.info("Serving %s at %s (Ctrl+C to stop)", args.usd_path, url)

        if not args.no_open:
            webbrowser.open(url)

        try:
            server.serve_forever()
        except KeyboardInterrupt:
            logger.info("Stopping server")
        finally:
            server.server_close()

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
    "voxelize": (
        _add_voxelize_arguments,
        _run_voxelize,
        "Voxelize a converted USD stage (or IFC file) into occupancy voxel JSON.",
    ),
    "export-gltf": (
        _add_export_gltf_arguments,
        _run_export_gltf,
        "Export a converted USD stage to a glTF (GLB) file.",
    ),
    "serve": (
        _add_serve_arguments,
        _run_serve,
        "Serve a converted USD stage as a local web viewer.",
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
