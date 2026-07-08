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
uv run ifc2usd space-voxelize <ifc> --reference <usda> --size 0.5 -o <out.json>  # E9-5 space heatmap prerequisite
uv run ifc2usd serve <usda>                                # local web viewer, http://127.0.0.1:8000
uv run ifc2usd serve <usda> --twin twin-config.json --space-voxels <out.json>    # E9-3/E9-5 digital twin mode
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
  `get_space_geometry()` (E9-5) is the inverse path: it yields ONLY IfcSpace geometry, since
  the space/voxel heatmap is an additive asset that must never touch the canonical USD/GLB
  `get_geometry()` produces. It applies the local→world transform itself
  (`rotation.dot(vert) + offset`, the same composition `usd.append_prim()` does) rather than
  going through a USD Xform, since spaces never enter that tree. Inherits `get_geometry()`'s
  existing `--y-up` limitation: the Y/Z swap is applied to local vertices before combining with
  the (un-swapped) rotation matrix, which is only correct for 0°/180° yaw — a pre-existing
  pipeline-wide quirk, not something this function introduces or fixes.
- `usd.py` — USD stage construction. `build_stage()` walks the IFC spatial tree and calls
  `append_prim()` (Xform + optional mesh) for each node; `create_materials()` builds the
  `/Materials` scope up front so meshes can bind to them. `elements_from_stage()` reads a
  *converted* USD stage back out into `VoxelElement`s (world-space vertices, color, GUID/class)
  for `voxelize`/`serve` to consume, keyed on the same `GUID`/`class` customData + `mesh` child
  convention `append_prim()` writes.
- `voxel.py` — surface/interior voxelization (`voxelize_mesh`, AABB-vs-grid-cell overlap on
  numpy arrays), self-implemented Morton (Z-order) encode/decode, `build_voxel_json()` (spec.md
  §2 JSON v3), and `build_voxel_stage()` (spec.md §3 PointInstancer layer — one prototype Cube
  per element, one `voxelLOD` variant per `--size`, referencing the canonical USD without
  modifying it). Writes via `Usd.Stage.CreateNew(output_path)` + `GetRootLayer().Save()`, not
  `Stage.Export()`, because `Export()` flattens to the currently-selected variant and would
  discard the other LODs. `fill=True` interior detection is a pure voxel-grid exterior
  flood-fill (`_exterior_voxels`/`_fill_voxels`), not `trimesh.contains()` ray-casting — the
  latter proved unreliable on real non-manifold multi-body geometry (Issue #36 / E7-2). Each
  element's `indices` in the JSON are delta+RLE encoded (`encode_morton_indices`/
  `decode_morton_indices`, Issue #38 / E7-4) rather than a flat integer list; `viewer.js`'s
  `decodeMortonIndices` decodes that form but also passes a plain array through unchanged, so
  v2-shaped `indices` (older files, or the client-side v1→current converter) still load.
- `space_heatmap.py` (E9-5) — space/voxel heatmap aggregation. `build_space_voxel_index()`
  voxelizes each `IfcSpace` (`fill=True`) into a `{morton_code: spaceGuid}` map; overlapping
  cells (adjacent-space boundaries) go to whichever space has fewer filled voxels (a volume
  proxy — exact for box shapes, only approximate for complex/non-manifold ones per the same
  flood-fill limitation `voxel.py` already documents), with a deterministic GUID-string
  tiebreak so equal-count ties don't depend on `ifcopenshell`'s non-deterministic parallel
  iterator order. `build_space_voxel_json()` writes that *already-resolved* assignment out in
  the same v3 schema `voxel.build_voxel_json()` uses (so it rides the viewer's existing
  InstancedMesh/LOD code) — unlike `build_voxel_json()`, it does not re-voxelize each element
  independently, which is what keeps a contested boundary cell from appearing in two spaces'
  `indices` at once. `aggregate_values_by_space()`/`aggregate_values_by_storey()` (mean/min/
  max/count, explicitly excluding `bool` — a subclass of `int` in Python — from numeric
  aggregation) are the Python side of the same aggregation `viewer.js` re-implements
  client-side for live rendering (see below); the Storey fallback exists for models with no
  `IfcSpace` geometry (digital-twin-spec.md §5.4).
- `sdf.py` — `build_narrow_band_sdf()` builds a sparse signed-distance field from an occupancy
  voxel grid: dilates the surface voxel set by `band_width` cells (pure-Python 26-neighbor
  dilation, no scipy dependency) and computes brute-force nearest-surface distance via numpy
  broadcasting for every candidate. Returns a frozen `NarrowBandSDF` (`values`/`surface_voxels`/
  `origin`/`size` bundled together so callers can't mismatch `size`/`origin` between build and
  query). `clearance(point, sdf)` answers an arbitrary world point: O(1) dict lookup inside the
  band, falling back to a direct (unbounded, always-correct) computation against
  `surface_voxels` outside it.
- `sdf_slice.py` — `build_sdf_slices_json()` turns `sdf.py`'s per-element narrow-band SDF into
  dense 2D horizontal-slice grids (`sdf.values.get((ix, iy, iz))` directly, no `clearance()`
  fallback call — cells outside the band are `None`/transparent rather than triggering an
  unbounded per-cell direct computation) for the web viewer to render as a color-mapped overlay
  plane. A `_MAX_GRID_CELLS` cap skips (with a warning, not silently) any element whose XY
  footprint at the requested voxel size would produce an oversized grid; a separate
  `_MAX_SURFACE_VOXELS` cap catches what the footprint cap can't — a tall/thin element (small
  XY footprint, large Z extent) that would still drive `build_narrow_band_sdf`'s brute-force
  distance computation (which scales with 3D surface-voxel count, not XY footprint) into
  excessive memory/CPU.
- `gltf.py` — USD→glTF(GLB) via trimesh. Walks the USD prim tree from the default prim,
  building a `trimesh.Scene` graph using each prim's own local transform; explodes deduplicated
  mesh points through the face-vertex-index array so per-corner normals line up 1:1. Writes
  `extras.guid`/`class`/`name` on each node (the join key `scene_index.py`/the viewer use).
- `scene_index.py` — USD→`scene.json` (spec.md §4.1): denormalizes the spatial tree plus
  customData for the web viewer, which never parses USD directly.
- `serve.py` — `build_serve_directory()` assembles a self-contained static directory (GLB,
  scene.json, voxels.json when there's voxelizable geometry, `<stem>_sdf.json` when
  `sdf_slices=True`, `<stem>_twin.json` when a `twin` dict is given (E9-3), vendored `viewer/`
  assets); `make_server()` returns an unstarted `ThreadingHTTPServer` bound to `127.0.0.1` only,
  with directory listing disabled. Unlike voxels.json, SDF slices are opt-in
  (`sdf_slices=False` default / CLI `--sdf-slices`): they cost an extra per-element
  voxelize+narrow-band-SDF pass beyond what voxels.json already does. `twin.json` is the same
  "additive asset" shape but never carries live values or credentials (those stay in the
  `--twin twin-config.json` file and `twin_proxy.py`'s `TwinProxy`, respectively). When
  `make_server(..., twin_proxy=...)` is given a `TwinProxy` (built from `twin_proxy.load_twin_config()`
  + `ifc.py`/`mapping.py`-derived bindings), the returned server additionally whitelists
  `GET /api/twin/values?metric=` and `GET /api/twin/history?...` as same-origin proxy endpoints
  onto the Building OS REST API (`twin.py`'s `TwinClient`) — no other upstream endpoint (in
  particular no control API) is ever reachable through it. `TwinProxy.get_values()` caches per
  metric with a TTL (`=pollIntervalSeconds`), isolates per-point upstream failures so one flaky
  sensor doesn't blank out the whole metric, and falls back to the last good value (`stale: true`)
  only when every point in that metric's refresh failed; proxy error bodies returned to the
  browser never include the upstream Building OS URL (logged server-side only) per
  digital-twin-spec.md §6. Omitting `twin_proxy` (the default) leaves `make_server()`'s behavior
  byte-for-byte identical to before E9-3. `<stem>_space_voxels.json` (E9-5) is the same
  additive-asset shape when a `space_voxels` dict is given (`assets.spaceVoxels`); unlike
  voxels/sdf/twin, `build_serve_directory()` cannot compute it itself (IfcSpace geometry isn't
  in the canonical USD it opens), so the caller must pre-build it via `space_heatmap.
  build_space_voxel_json()` — in practice the `ifc2usd space-voxelize` CLI subcommand, run
  once against the original `.ifc` + the already-converted reference USD, with its output path
  passed to `serve --space-voxels`. Because that JSON is built independently and must share
  the *same* scene origin/upAxis as the model's own voxels.json (digital-twin-spec.md §5.4),
  `build_serve_directory()` cross-checks both and logs a warning (not a hard failure — matching
  the "additive asset, trust the caller" posture used elsewhere) if they've drifted apart, e.g.
  from voxelizing against a stale reference.
- `viewer/viewer.js` — three.js web viewer (ES modules, no build step; three.js is vendored
  under `viewer/vendor/`, not CDN-loaded). GLB display, OrbitControls camera, hierarchy tree
  with visibility toggles, click-to-select (mesh and voxel, via `Raycaster` + GUID reverse
  lookup) synced bidirectionally with the tree and a property panel, voxel rendering as one
  `InstancedMesh` per LOD, a mesh/voxel/both display-mode + LOD switch UI, and (when
  `scene.json`'s `assets.sdf` is present) a per-element SDF horizontal-slice overlay toggle +
  height slider shown only while that element is selected. When `assets.twin` is present
  (E9-4), a "Live" toolbar group (hidden entirely otherwise, same additive-asset convention as
  SDF slices) lets the user pick a metric and toggle live polling of `/api/twin/values`; a
  self-contained turbo-colormap polynomial approximation (`_turboColor`/`TURBO_LUT`, Anton
  Mikhailov/Google Research, Apache-2.0 — no external asset/CDN) maps each bound element's
  latest value to a color via the same `_ensureOwnMaterial` clone-on-write strategy E8-1 uses,
  desaturating stale values per digital-twin-spec.md §5.2. The color-application math is
  factored into `applyColorMappedValues`/`_valueToLiveColor`, which take an explicit `nowMs`
  parameter rather than reading `Date.now()` internally, specifically so E9-6 (playback) can
  call the same function with a historical frame's timestamp instead of duplicating the
  min/max/LUT/staleness logic (digital-twin-spec.md §5.5 requires live and playback to share
  one color-application function). The legend's gradient bar is a CSS `linear-gradient` on a
  plain `<div>`, not a `<canvas>` — a `<canvas>` placed inside `#viewport` was tried first and
  broke every existing Playwright pixel-sampling test, because `document.querySelector('#viewport
  canvas')` (the pattern all of them use to grab the WebGL surface) matched that new canvas
  first in DOM order instead of the renderer's own canvas (which `viewport.appendChild(renderer.domElement)`
  only appends at JS runtime, i.e. later in document order). Any future overlay UI added inside
  `#viewport` must avoid introducing a second `<canvas>` element there for the same reason.
  `_setLiveColorForGuid`/`clearLiveColors` must not write to a mesh whose `.material` currently
  *is* the shared `_ghostMaterial` singleton (E8-1) — doing so recolors every ghosted element in
  the scene at once, not just the live-bound one; both functions check
  `mesh.material === _ghostMaterial` and skip (matching `selectByGuid`'s own pre-existing
  un-ghost-before-touching-color-or-emissive guard) rather than writing through it. When
  `assets.spaceVoxels` is present (E9-5), a second `InstancedMesh`-per-LOD layer
  (`spaceVoxelRoot`/`buildSpaceVoxelLods`, same `_voxelUnitBox` geometry as the regular voxel
  layer but its own per-LOD material so the two never share color state) renders room-level
  heatmap voxels; visibility piggybacks on the existing "Live" toggle and display-mode radios
  rather than adding new toolbar UI. `applySpaceValues()` deliberately does not recompute the
  turbo-LUT/staleness math itself — it aggregates `spaceGuid`-tagged values from the same
  `/api/twin/values` poll E9-4 already uses (mean, tracking the *oldest* contributing
  `datetime` so a stale reading anywhere in a room still triggers `_valueToLiveColor`'s
  desaturation) and calls E9-4's shared `_valueToLiveColor()`, per digital-twin-spec.md §5.5's
  requirement that live and playback (and now the space heatmap) share one color-application
  function rather than each reimplementing it. For models with no `IfcSpace` geometry at all
  (no `assets.spaceVoxels`), the same function falls back to coloring every element mesh under
  a Storey via `_setLiveColorForGuid` when a binding's `spaceGuid` resolves to that Storey's own
  GUID (`_buildStoreyDescendantIndex`, built once from `scene.json`'s tree) — digital-twin-spec.md
  §5.4's Storey-level fallback, reusing E9-4's per-object coloring instead of a separate code path.
  E9-6 (time-series playback) adds a "Playback" toolbar group (same `assets.twin`-gated,
  hidden-otherwise convention) that fetches `/api/twin/history` once for every point bound to
  the selected metric (`_loadPlaybackFrames`, `Promise.allSettled` so one point's fetch failure
  doesn't discard the others' data — mirroring the per-point error isolation
  `twin_proxy.TwinProxy.get_values()` already does server-side), bins the results into a sorted
  frame list (`_buildPlaybackFrames`), then lets a slider/play-button scrub through them with
  zero further network calls. `_applyPlaybackFrame` calls the *same*
  `applyColorMappedValues`/`applySpaceValues` functions E9-4/E9-5 use, passing the frame's own
  timestamp as `nowMs` instead of `Date.now()` — this is the reason those functions took an
  explicit `nowMs` parameter in the first place (digital-twin-spec.md §5.5). Because Live
  polling and Playback scrubbing both ultimately write through `_setLiveColorForGuid`/
  `applySpaceValues`, the two must never run concurrently: enabling Live stops any running
  playback `setInterval` and vice versa (`_loadPlaybackFrames` unchecks `#live-toggle` before
  fetching; the `liveToggle` "change" handler calls `_stopPlayback()` before starting live
  polling). Loading playback frames also invalidates any previously-loaded frames from a
  different metric — `liveMetricSelect`'s "change" handler resets the slider back to disabled
  rather than leaving stale frames from the old metric paired with the new metric's legend/unit.
  `_updateSpaceVoxelVisibility()` gates the E9-5 space-heatmap layer on
  `liveEnabled || playbackFrames.length > 0`, not `liveEnabled` alone — otherwise the space
  heatmap would never render during playback, since loading playback frames always forces Live
  off first.

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
- The SDF slice overlay plane (`updateSdfSliceOverlay` in `viewer.js`) sets `depthTest: false`
  and a high `renderOrder`: it slices through the interior of the very element it's describing,
  so with normal depth testing it's invisible, occluded by that element's own opaque mesh/voxel
  geometry. This is deliberate — it's a diagnostic overlay meant to always be visible on top,
  not a physically depth-composited object. Any future "cut through this element" overlay should
  use the same pattern rather than relying on depth testing to make it visible.
- Morton codes in `voxel.py`/`viewer.js` can be up to 63 bits (spec.md §2, 21 bits/axis). JS's
  native `<<`/`>>`/`&`/`|` truncate to 32-bit signed ints and *wrap the shift amount mod 32*
  rather than saturating — `mortonDecode()` in `viewer.js` uses a fast plain-Number path only
  below a threshold where the loop's shifts can't reach 32 (2^30-1, not 2^31-1 — see the comment
  there for the exact math), falling back to BigInt above it.
- `gltf.py`'s `_mesh_material_properties()` must read `metallic`/`roughness` off the bound
  `UsdPreviewSurface` shader (falling back to `0.0`/`1.0`, matching `usd.py`'s own defaults) and
  pass them through as `metallicFactor`/`roughnessFactor` on the exported `PBRMaterial` — never
  omit them. glTF's spec default when they're absent is `1.0`/`1.0` (fully metallic), and a
  metallic material has no diffuse reflectance; without an environment map (this viewer has
  none) it renders essentially black under plain directional/hemisphere light regardless of a
  correct `baseColorFactor`. This was a real, previously-undetected bug (every element rendered
  near-black) caught only once a Playwright test started sampling actual rendered pixel colors
  instead of just checking exported glTF JSON or USD data — checking data correctness is not
  the same as checking it actually reaches the screen looking right.
- The voxel `InstancedMesh` in `buildVoxelLods()` must **not** set `vertexColors: true` on its
  material. `object.instanceColor !== null` alone is enough for three.js to enable
  `USE_INSTANCING_COLOR` in the shader — it does not depend on `material.vertexColors`. But
  `vertexColors: true` *additionally* enables `USE_COLOR`, which requires a geometry-level
  per-vertex `color` attribute; the shared `_voxelUnitBox` `BoxGeometry` has none, so the
  unbound `color` GLSL attribute reads WebGL's disabled-attribute default `(0,0,0,1)`, and the
  `color_vertex` shader chunk does `vColor = vec3(1.0); vColor *= color;` — zeroing `vColor`
  — *before* `vColor.xyz *= instanceColor.xyz` ever runs, so every voxel rendered black
  regardless of the color passed to `setColorAt()` (Issue #39 / E8-6). Same lesson as the
  `gltf.py` metallic/roughness bug above: this was only caught once a Playwright test sampled
  actual rendered pixels instead of just the `instanceColor` buffer contents.

## Planned work

`docs/viewer/` holds the research, architecture, spec, and backlog for a USD + voxel viewer
(Hydra-inspired: author PointInstancer/variant/purpose USD layers for external Hydra viewers,
plus the self-contained three.js web viewer in `ifc2usd/viewer/`, served by `ifc2usd serve`).
Sprints 1-4 of the backlog (`docs/viewer/backlog.md`) are implemented: voxelization, glTF
export, and the full viewer MVP (tree, selection, voxel rendering, display modes), plus the
follow-up items (section clip plane, Playwright regression coverage audit, usdview/Blender/
Omniverse verification checklists, payload lazy-load measurement). Of Epic E5 (P2/future,
volume fields and analysis display): E5-1 (narrow-band SDF, `sdf.py`) and a scope-reduced E5-3
(SDF horizontal-slice overlay, `sdf_slice.py` + `serve --sdf-slices`) are implemented; E5-2
(UsdVol+OpenVDB output) and full GPU raymarching are blocked on OpenVDB having no
pip-installable Python bindings in this environment (see the note under Epic E5 in
`docs/viewer/backlog.md`); Epic E6 is untouched. Epic E7 (voxelization/rendering quality) is
fully done: E7-1 vectorization, E7-2 flood-fill interior detection, E7-3 voxel selection
highlight, E7-4 Morton index delta+RLE compression, plus a wireframe toggle and the
voxel-near-black rendering bug fix (Issue #39). Epic E8 (viewer UX/design overhaul,
`docs/viewer/ux-spec.md` — outline highlighting, tree/3D linkage, tree/property panel
improvements, toolbar grouping/design tokens/keyboard shortcuts) is fully done (E8-1 through
E8-6). Epic E9 (building-OS digital twin mode, `docs/viewer/digital-twin-spec.md` — GUTP
Building OS RI integration; note the researched fact that its data model carries no IFC
GUIDs, so the GUID↔point mapping layer is ours) is fully done, E9-1 through E9-6: `twin.py`
(Building OS REST adapter + `twin.json` schema), `mapping.py` (mapping.json's 3 generator
paths), `twin_proxy.py` + `serve --twin` (whitelisted same-origin proxy with per-metric
TTL/stale caching), the viewer's "Live" object color-mapping + legend + Live Data panel,
`space_heatmap.py` + `ifc2usd space-voxelize` + the viewer's room-level voxel heatmap
(E9-5, which finally implements E5-4 / closes Issue #30), and the viewer's "Playback" toolbar
group for scrubbing through `/api/twin/history` (E9-6). Consult `docs/viewer/spec.md` before
extending viewer-related features.

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
