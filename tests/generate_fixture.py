"""最小の IFC フィクスチャを生成する。

ifcopenshell 0.8 の API で、変換テストに必要な最小構成
(Project → Site → Building → Storey に、マテリアル付きの壁を配置) を作る。
外部の IFC データに依存せず、変換パイプラインを検証できるようにするのが目的。

    uv run python tests/generate_fixture.py
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

import ifcopenshell
import ifcopenshell.api.aggregate
import ifcopenshell.api.context
import ifcopenshell.api.geometry
import ifcopenshell.api.project
import ifcopenshell.api.root
import ifcopenshell.api.spatial
import ifcopenshell.api.style
import ifcopenshell.api.unit
import ifcopenshell.util.shape_builder

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "minimal.ifc"


def _add_wall(model, body, storey, builder, name, origin, size, colour):
    """押し出しソリッドの壁を1枚作り、色を付けて storey に配置する。"""
    wall = ifcopenshell.api.root.create_entity(model, ifc_class="IfcWall", name=name)

    profile = builder.rectangle(size=np.array([size[0], size[1]]))
    solid = builder.extrude(profile, magnitude=size[2], position=np.array(origin, dtype=float))
    representation = builder.get_representation(body, [solid])
    ifcopenshell.api.geometry.assign_representation(model, product=wall, representation=representation)

    style = ifcopenshell.api.style.add_style(model, name=f"{name}_style")
    ifcopenshell.api.style.add_surface_style(
        model,
        style=style,
        ifc_class="IfcSurfaceStyleShading",
        attributes={
            "SurfaceColour": {"Name": None, "Red": colour[0], "Green": colour[1], "Blue": colour[2]},
            "Transparency": 0.0,
        },
    )
    ifcopenshell.api.style.assign_representation_styles(
        model, shape_representation=representation, styles=[style]
    )

    ifcopenshell.api.spatial.assign_container(model, products=[wall], relating_structure=storey)
    return wall


def build() -> ifcopenshell.file:
    model = ifcopenshell.api.project.create_file(version="IFC4")

    project = ifcopenshell.api.root.create_entity(model, ifc_class="IfcProject", name="Fixture Project")
    # 既定は mm。寸法を m 単位で扱えるよう SI メートルを明示する
    metre = ifcopenshell.api.unit.add_si_unit(model, unit_type="LENGTHUNIT")
    ifcopenshell.api.unit.assign_unit(model, units=[metre])

    context = ifcopenshell.api.context.add_context(model, context_type="Model")
    body = ifcopenshell.api.context.add_context(
        model, context_type="Model", context_identifier="Body", target_view="MODEL_VIEW", parent=context
    )

    site = ifcopenshell.api.root.create_entity(model, ifc_class="IfcSite", name="Fixture Site")
    building = ifcopenshell.api.root.create_entity(model, ifc_class="IfcBuilding", name="Fixture Building")
    storey = ifcopenshell.api.root.create_entity(model, ifc_class="IfcBuildingStorey", name="Ground Floor")

    ifcopenshell.api.aggregate.assign_object(model, products=[site], relating_object=project)
    ifcopenshell.api.aggregate.assign_object(model, products=[building], relating_object=site)
    ifcopenshell.api.aggregate.assign_object(model, products=[storey], relating_object=building)

    builder = ifcopenshell.util.shape_builder.ShapeBuilder(model)
    _add_wall(model, body, storey, builder, "Wall North", (0.0, 0.0, 0.0), (5.0, 0.2, 3.0), (0.8, 0.2, 0.2))
    _add_wall(model, body, storey, builder, "Wall East", (5.0, 0.0, 0.0), (0.2, 4.0, 3.0), (0.2, 0.5, 0.8))

    return model


def main() -> None:
    model = build()
    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    model.write(str(FIXTURE_PATH))
    print(f"Wrote fixture: {FIXTURE_PATH}")
    print(f"  walls: {len(model.by_type('IfcWall'))}, styles: {len(model.by_type('IfcSurfaceStyleShading'))}")


if __name__ == "__main__":
    main()
