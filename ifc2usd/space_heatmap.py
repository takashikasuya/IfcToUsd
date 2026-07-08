"""空間(IfcSpace)単位のボクセルヒートマップ集計（Epic E9 / E9-5）。

`docs/viewer/digital-twin-spec.md` §5.4 に対応する。各IfcSpaceを充填ボクセル化
（`voxel.py`の`voxelize_mesh`, ``fill=True``）し、重複するボクセルセル（隣接
空間の境界等）は充填ボクセル数が少ない=体積の小さい空間を優先して1つの
spaceGuidへ帰属させる。`mapping.json`の`spaceGuid`バインディングで得た値を
その帰属マップに沿って集計し（既定は平均、min/max/countも選択可）、部屋別の
集計値を`voxels.json`と同じv3スキーマ・同じシーン共有originでボクセル色として
描画できるようにする（既存のボクセル描画・LOD切替にそのまま乗る）。

空間ジオメトリが取れないモデル（IfcSpace未定義）向けに、Storey単位の
フォールバック集計も提供する。
"""

from __future__ import annotations

from typing import Mapping, Optional, Sequence

from .voxel import VoxelElement, encode_morton_indices, morton_encode, voxelize_mesh

_JSON_VERSION = 3  # voxel.pyのボクセルJSON(v3)スキーマと同一に保つこと
_UNITS = "m"

_AGGREGATIONS = ("mean", "min", "max", "count")


def build_space_voxel_index(
    spaces: Sequence[VoxelElement],
    size: float,
    origin: Sequence[float],
) -> dict[int, str]:
    """各空間を充填ボクセル化し、ボクセル（Morton符号）→spaceGuidの対応表を作る。

    複数の空間が同じセルを占有する場合（隣接空間の境界面など）、充填ボクセル数
    が少ない方＝体積の小さい空間を優先する（小部屋が大部屋に飲み込まれて
    消えてしまうのを防ぐ）。`origin`は呼び出し側が用意するシーン共有origin
    （voxels.jsonと同一）で、各空間自身のAABB最小点以下である必要がある
    （`voxelize_mesh`の制約）。

    体積の近似に充填ボクセル数を使うのは単純な直方体形状では正確だが、複雑な
    非多様体形状では`voxelize_mesh`のフラッドフィルが内部を過小充填しうる
    （Issue #36 / E7-2と同種の既知の限界）ため、極端に複雑な形状では優先順位が
    真の体積と逆転する可能性がある。
    """
    per_space_voxels: dict[str, set[tuple[int, int, int]]] = {}
    for space in spaces:
        if not len(space.vertices):
            continue
        _, voxels = voxelize_mesh(space.vertices, space.indices, size, origin=origin, fill=True)
        if voxels:
            per_space_voxels[space.guid] = voxels

    # 充填ボクセル数の多い順に書き込み、少ない（=小さい）空間を最後に上書き
    # させることで優先させる。件数が同点の場合はguidの昇順順で決着させる
    # （`spaces`の入力順——ifcopenshellの並列イテレータ由来で実行ごとに変わりうる
    # ——に依存させず、常に同じ結果を再現できるようにするため）。
    ordered_guids = sorted(
        per_space_voxels, key=lambda guid: (len(per_space_voxels[guid]), guid), reverse=True
    )

    index: dict[int, str] = {}
    for guid in ordered_guids:
        for voxel in per_space_voxels[guid]:
            index[morton_encode(*voxel)] = guid
    return index


def build_space_voxel_json(
    spaces: Sequence[VoxelElement],
    sizes: Sequence[float],
    origin: Sequence[float],
    source: Optional[dict] = None,
    up_axis: str = "Z",
) -> dict:
    """空間ごとの充填ボクセルを、既存の`voxels.json`（v3）と同じスキーマ・
    origin規約で書き出す（ビューワーの既存ボクセル描画・LOD切替にそのまま
    乗せるため）。`voxel.build_voxel_json`と異なり各要素を独立に再ボクセル化
    しない——重複セルを`build_space_voxel_index`で解決した後の帰属だけを
    出力するため、隣接空間の境界セルが2つの空間で二重に描画されることがない。

    頂点を持たない、または充填ボクセルが1つも無い空間は`indices`を空のまま
    出力する（`build_voxel_json`と同じ理由: 他のLODに存在するのにこのLODでは
    要素自体が消えるとビューワー側で区別が付かなくなるため）。
    """
    unique_sizes = list(dict.fromkeys(sizes))

    lods = []
    for size in unique_sizes:
        voxel_index = build_space_voxel_index(spaces, size, origin)
        codes_by_guid: dict[str, list[int]] = {}
        for code, guid in voxel_index.items():
            codes_by_guid.setdefault(guid, []).append(code)

        lod_elements = []
        for space in spaces:
            codes = sorted(codes_by_guid.get(space.guid, []))
            lod_elements.append(
                {
                    "guid": space.guid,
                    "class": space.cls,
                    "name": space.name,
                    "color": list(space.color),
                    "indices": encode_morton_indices(codes),
                }
            )
        lods.append({"size": size, "elements": lod_elements})

    return {
        "version": _JSON_VERSION,
        "units": _UNITS,
        "upAxis": up_axis,
        "source": source or {},
        "origin": list(origin),
        "lods": lods,
    }


def aggregate_values_by_space(
    entries: Sequence[Mapping[str, object]],
    aggregation: str = "mean",
) -> dict[str, dict[str, object]]:
    """`entries`（各`{"spaceGuid", "value", "unit"?}`、`TwinProxy.get_values()`が
    返す束のうち`spaceGuid`付きのもの）をspaceGuidごとに集計する。

    Args:
        aggregation: `"mean"`（既定）/ `"min"` / `"max"` / `"count"`。

    Returns:
        `{spaceGuid: {"value": 集計値, "count": 件数, "unit": 単位（先頭値優先）}}`。
        数値が1件も無い空間は`"value": None`になる（`"count"`集計時を除く）。
    """
    if aggregation not in _AGGREGATIONS:
        raise ValueError(f"unsupported aggregation: {aggregation!r}")

    grouped: dict[str, list[Mapping[str, object]]] = {}
    for entry in entries:
        space_guid = entry.get("spaceGuid")
        if not space_guid:
            continue
        grouped.setdefault(space_guid, []).append(entry)

    result: dict[str, dict[str, object]] = {}
    for space_guid, group in grouped.items():
        # bool は int のサブクラスのため isinstance(v, (int, float)) だけだと
        # 真偽値のメトリック（在室/警報等）が1/0として平均・min/max計算に
        # 紛れ込んでしまう。数値集計の対象からは明示的に除く。
        values = [
            e["value"]
            for e in group
            if isinstance(e.get("value"), (int, float)) and not isinstance(e.get("value"), bool)
        ]
        count = len(values)

        if aggregation == "count":
            agg_value = count
        elif count == 0:
            agg_value = None
        elif aggregation == "mean":
            agg_value = sum(values) / count
        elif aggregation == "min":
            agg_value = min(values)
        else:  # "max"
            agg_value = max(values)

        unit = next((e.get("unit") for e in group if e.get("unit")), None)
        result[space_guid] = {"value": agg_value, "count": count, "unit": unit}

    return result


def aggregate_values_by_storey(
    entries: Sequence[Mapping[str, object]],
    guid_to_storey_guid: Mapping[str, str],
    aggregation: str = "mean",
) -> dict[str, dict[str, object]]:
    """空間ジオメトリが取れないモデル向けのフォールバック集計。

    `entries`の各エントリの`spaceGuid`（無ければ`guid`）を、呼び出し側が用意した
    要素GUID→Storey GUIDの対応表（`scene.json`のツリー階層から導出する想定）で
    Storey単位にグループ化した上で`aggregate_values_by_space`と同じ集計を行う。
    """
    remapped = []
    for entry in entries:
        key = entry.get("spaceGuid") or entry.get("guid")
        storey_guid = guid_to_storey_guid.get(key)
        if storey_guid is None:
            continue
        remapped.append({**entry, "spaceGuid": storey_guid})

    return aggregate_values_by_space(remapped, aggregation=aggregation)
