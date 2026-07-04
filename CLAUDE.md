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
uv run ifc2usd voxelize <usda|ifc> --size 1.0 --size 0.5   # -> <base>.json + <base>.usda
uv run ifc2usd export-gltf <usda> -o <out.glb>
uv run ifc2usd serve <usda>                                # local web viewer, http://127.0.0.1:8000
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

- `cli.py` — argparse entry point with four subcommands (`convert`/`voxelize`/`export-gltf`/
  `serve`; bare `ifc2usd <ifc>` is back-compat for `convert`). `convert()` reads all geometry
  into a `geometries` dict keyed by IFC GlobalId, accumulates a `materials` dict, then hands
  both to the USD writer.
- `ifc.py` — IFC extraction via `ifcopenshell.geom`. `get_geometry()` is a generator that
  yields `(verts, indices, norms, info, material_name, diffuse_color, matrix)` per element,
  skipping opening/space/zone elements. `get_properties()` flattens IFC property sets.
- `usd.py` — USD stage construction. `build_stage()` walks the IFC spatial tree and calls
  `append_prim()` (Xform + optional mesh) for each node; `create_materials()` builds the
  `/Materials` scope up front so meshes can bind to them. `elements_from_stage()` reads a
  *converted* USD stage back out into `VoxelElement`s (world-space vertices, color, GUID/class)
  for `voxelize`/`serve` to consume, keyed on the same `GUID`/`class` customData + `mesh` child
  convention `append_prim()` writes.
- `voxel.py` — surface/interior voxelization (`voxelize_mesh`, AABB-vs-grid-cell overlap on
  numpy arrays), self-implemented Morton (Z-order) encode/decode, `build_voxel_json()` (spec.md
  §2 JSON v2), and `build_voxel_stage()` (spec.md §3 PointInstancer layer — one prototype Cube
  per element, one `voxelLOD` variant per `--size`, referencing the canonical USD without
  modifying it). Writes via `Usd.Stage.CreateNew(output_path)` + `GetRootLayer().Save()`, not
  `Stage.Export()`, because `Export()` flattens to the currently-selected variant and would
  discard the other LODs.
- `gltf.py` — USD→glTF(GLB) via trimesh. Walks the USD prim tree from the default prim,
  building a `trimesh.Scene` graph using each prim's own local transform; explodes deduplicated
  mesh points through the face-vertex-index array so per-corner normals line up 1:1. Writes
  `extras.guid`/`class`/`name` on each node (the join key `scene_index.py`/the viewer use).
- `scene_index.py` — USD→`scene.json` (spec.md §4.1): denormalizes the spatial tree plus
  customData for the web viewer, which never parses USD directly.
- `serve.py` — `build_serve_directory()` assembles a self-contained static directory (GLB,
  scene.json, voxels.json when there's voxelizable geometry, vendored `viewer/` assets);
  `make_server()` returns an unstarted `ThreadingHTTPServer` bound to `127.0.0.1` only, with
  directory listing disabled.
- `viewer/viewer.js` — three.js web viewer (ES modules, no build step; three.js is vendored
  under `viewer/vendor/`, not CDN-loaded). GLB display, OrbitControls camera, hierarchy tree
  with visibility toggles, click-to-select (mesh and voxel, via `Raycaster` + GUID reverse
  lookup) synced bidirectionally with the tree and a property panel, voxel rendering as one
  `InstancedMesh` per LOD, and a mesh/voxel/both display-mode + LOD switch UI.

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
- **three.js `Raycaster` does not respect ancestor `.visible`** — it only checks each node's
  own flag when that node's own `raycast()` runs, and a `Group`'s `raycast()` is a no-op. Toggling
  a parent Group's `.visible` (e.g. `viewer.js`'s `glbRoot`/mesh-vs-voxel display mode) hides it
  visually but does *not* stop raycasting into its still-`visible=true` children. Click-to-select
  builds its raycast target list explicitly from display-mode state (`currentRaycastTargets()`)
  rather than relying on `.visible` propagation — do the same for any new pickable layer.
- Morton codes in `voxel.py`/`viewer.js` can be up to 63 bits (spec.md §2, 21 bits/axis). JS's
  native `<<`/`>>`/`&`/`|` truncate to 32-bit signed ints and *wrap the shift amount mod 32*
  rather than saturating — `mortonDecode()` in `viewer.js` uses a fast plain-Number path only
  below a threshold where the loop's shifts can't reach 32 (2^30-1, not 2^31-1 — see the comment
  there for the exact math), falling back to BigInt above it.

## Planned work

`docs/viewer/` holds the research, architecture, spec, and backlog for a USD + voxel viewer
(Hydra-inspired: author PointInstancer/variant/purpose USD layers for external Hydra viewers,
plus the self-contained three.js web viewer in `ifc2usd/viewer/`, served by `ifc2usd serve`).
Sprints 1-4 of the backlog (`docs/viewer/backlog.md`) are implemented: voxelization, glTF
export, and the full viewer MVP (tree, selection, voxel rendering, display modes). Consult
`docs/viewer/spec.md` before extending viewer-related features; remaining backlog items
(section clip plane, broader Playwright regression coverage, Hydra/usdview/Omniverse
verification, large-model payload streaming) are still open.

## Out of scope

`IFC_to_GLTF.ipynb`, `IFC_to_RDF.ipynb`, `GLTF_to_Voxel.ipynb` are separate notebook pipelines
not covered by the `ifc2usd` package or its dependencies.

## Token and usage discipline

- Prefer concise answers unless detailed reasoning is explicitly requested.
- Do not scan the whole repository by default.
- Before reading more than 10 files, list the candidate files and explain why they are needed.
- Before making broad changes, propose a plan first.
- Keep tool use focused on the files directly relevant to the task.
- Do not run tests repeatedly without explaining what changed.
- For simple edits, use low effort.
- For normal coding, use medium effort.
- Use high or xhigh only for difficult debugging, architecture decisions, or multi-file refactors.
- If the session becomes long, summarize the current state into a worklog file and recommend starting a fresh session.
