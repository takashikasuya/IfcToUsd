# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Converts IFC (building BIM) models into a structured OpenUSD stage. The IFC spatial
hierarchy (Site → Building → Storey → Space / Element → Object) is reproduced as a USD
Xform tree; each element carries a mesh, a UsdPreviewSurface material, and IFC-derived
metadata stored as USD `customData` (GUID, class, name, latitude/longitude).

## Commands

```bash
uv sync                                   # create .venv and install deps (incl. dev group)
uv run ifc2usd files/ToyodaLab.ifc        # convert -> output/<name>_structured.usda
uv run ifc2usd <ifc> -o <out.usda> --y-up --verbose
uv run python -m ifc2usd <ifc>            # equivalent module entry point
uv run pytest                             # run the end-to-end conversion tests
uv run python tests/generate_fixture.py   # regenerate tests/fixtures/minimal.ifc
```

Tests live in `tests/` and are the primary verification path. `tests/generate_fixture.py`
synthesizes a tiny IFC (two colored walls in a Site/Building/Storey) via the ifcopenshell 0.8
authoring API; `tests/test_convert.py` converts it and asserts on up-axis, hierarchy, world
extent, per-wall colors/material bindings, and customData. The committed
`tests/fixtures/minimal.ifc` lets tests run without the large `files/ToyodaLab.ifc`
(tracked, ~2.8MB) — use that real model for eyeballing bigger changes. No linter is configured.

## Architecture

The `ifc2usd/` package is the deliverable. It is a clean-room refactor of `IFC_to_USD.ipynb`
(kept for reference) into an order-independent CLI. Data flows in one pass — the notebook's
`pickle` cross-cell cache was removed.

- `cli.py` — argparse entry point + `convert()`. Reads all geometry into a `geometries` dict
  keyed by IFC GlobalId, accumulates a `materials` dict, then hands both to the USD writer.
- `ifc.py` — IFC extraction via `ifcopenshell.geom`. `get_geometry()` is a generator that
  yields `(verts, indices, norms, info, material_name, diffuse_color, matrix)` per element,
  skipping opening/space/zone elements. `get_properties()` flattens IFC property sets.
- `usd.py` — USD stage construction. `build_stage()` walks the IFC spatial tree and calls
  `append_prim()` (Xform + optional mesh) for each node; `create_materials()` builds the
  `/Materials` scope up front so meshes can bind to them.

### ifcopenshell 0.8 specifics (breaking vs. the old notebook API)

- Geometry settings use **string keys**: `settings.set("weld-vertices", False)`, not the old
  `settings.set(settings.WELD_VERTICES, ...)` enum form. See `create_settings()` in `ifc.py`.
- `shape.transformation.matrix` is a flat 16-element **column-major** 4x4 tuple (no `.data`).
  `_matrix12()` extracts the `[X, Y, Z, T]` 12-element form the transform math expects.
- Material style colors are `colour` objects accessed via `.r()/.g()/.b()` (not indexable);
  `_color_to_tuple()` converts them. Presence flags are callables: `mat.has_transparency()`.
  There is no `has_specular`/`has_diffuse`.

### Conventions to preserve

- **Z-UP by default** (IFC standard); `--y-up` swaps Y/Z on verts and normals and sets
  `UsdGeom.SetStageUpAxis` to Y. Keep the swap in `ifc.py` and the axis in `usd.py` in sync.
- Normals are negated on read (`n * -1`) to correct IfcOpenShell's orientation, then reordered
  to `faceVarying`; subdivision scheme is forced to `"none"`.
- Material names are sanitized for USD prim paths via `sanitize_material_name()` (hyphen→`_`,
  other punctuation dropped).

## Planned work

`docs/viewer/` holds the research, architecture, spec, and backlog for a USD + voxel viewer
(Hydra-inspired: author PointInstancer/variant/purpose USD layers for external Hydra viewers,
plus a self-contained three.js web viewer served by a future `ifc2usd serve`). Consult it
before implementing viewer-related features; the voxel JSON v2 schema and CLI subcommand
layout are specified there.

## Out of scope

`IFC_to_GLTF.ipynb`, `IFC_to_RDF.ipynb`, `GLTF_to_Voxel.ipynb` are separate notebook pipelines
not covered by the `ifc2usd` package or its dependencies.
