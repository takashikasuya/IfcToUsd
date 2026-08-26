# AGENTS.md

This project's full agent guidance (architecture, conventions, commands, gotchas) lives in
[CLAUDE.md](CLAUDE.md) — read it before making changes. This file exists only so tools that
look for `AGENTS.md` specifically pick up the same guidance.

## Quick commands

```bash
uv sync                # install deps
uv run ifc2usd files/ToyodaLab.ifc   # convert IFC -> USD
uv run pytest          # run tests (primary verification path)
```

See [CLAUDE.md](CLAUDE.md) for the full architecture overview, ifcopenshell 0.8 API quirks,
viewer/digital-twin epic status, and conventions to preserve (Z-up default, three.js Raycaster
visibility caveat, Morton code 32-bit truncation, glTF metallic/roughness defaults, voxel
InstancedMesh vertexColors pitfall, etc.).
