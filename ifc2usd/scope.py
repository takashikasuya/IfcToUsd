"""`convert` の範囲指定（階、または GlobalId の集合）。

範囲は「出力する要素の GlobalId の集合」に解決する。空間の階層（Site・Building・Storey・Space）は
範囲によらず保ち、範囲外の要素だけを出さない。

- 階（``storey``）：その IfcBuildingStorey と、集約でその下にある空間（IfcSpace など）に
  置かれた要素（IfcRelContainedInSpatialStructure）。
- GlobalId の集合（``global_ids``）：列挙された要素。

どちらも、選ばれた要素の集約の部品（IfcRelAggregates の子、再帰）を含める。窓のように形状を
部品だけが持つ要素を選んだときに、部品の形状が落ちないようにするため。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Iterator


@dataclass(frozen=True)
class ConvertScope:
    """出力の範囲。``storey`` と ``global_ids`` のどちらか一方だけを指定する。"""

    storey: str | None = None
    global_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (self.storey is None) == (not self.global_ids):
            raise ValueError("ConvertScope needs exactly one of storey or global_ids")

    def record(self) -> dict:
        """USD のルートの customData（``ifcScope``）に残す形。"""
        if self.storey is not None:
            return {"storey": self.storey}
        return {"globalIds": sorted(set(self.global_ids))}


def _is_spatial(entity) -> bool:
    # IFC2X3 には IfcSpatialElement が無い（IfcSpatialStructureElement だけ）
    return entity.is_a("IfcSpatialStructureElement") or entity.is_a("IfcSpatialElement")


def _by_guid(ifc_file, guid: str):
    try:
        return ifc_file.by_guid(guid)
    except RuntimeError:
        raise ValueError(f"GlobalId not found in the IFC file: {guid}") from None


def _aggregated_children(entity) -> Iterator:
    for rel in getattr(entity, "IsDecomposedBy", None) or []:
        yield from rel.RelatedObjects


def _with_parts(elements: Iterable) -> Iterator:
    stack = list(elements)
    while stack:
        element = stack.pop()
        yield element
        stack.extend(_aggregated_children(element))


def _storey_elements(storey) -> Iterator:
    """階と、集約でその下にある空間に置かれた要素（空間そのものも含む）。"""
    stack = [storey]
    while stack:
        spatial = stack.pop()
        yield spatial
        for rel in getattr(spatial, "ContainsElements", None) or []:
            yield from rel.RelatedElements
        stack.extend(child for child in _aggregated_children(spatial) if _is_spatial(child))


def resolve_scope(ifc_file, scope: ConvertScope) -> set[str]:
    """範囲を、出力する要素の GlobalId の集合へ解決する。見つからない GlobalId は ValueError。"""
    if scope.storey is not None:
        storey = _by_guid(ifc_file, scope.storey)
        if not storey.is_a("IfcBuildingStorey"):
            raise ValueError(f"{scope.storey} is a {storey.is_a()}, not an IfcBuildingStorey")
        roots = list(_storey_elements(storey))
    else:
        roots = [_by_guid(ifc_file, guid) for guid in scope.global_ids]
        for root in roots:
            if _is_spatial(root):
                raise ValueError(
                    f"{root.GlobalId} is a spatial element ({root.is_a()}); use the storey scope instead"
                )
    return {element.GlobalId for element in _with_parts(roots)}
