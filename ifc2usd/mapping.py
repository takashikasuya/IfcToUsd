"""`mapping.json`（ポイント⇔BIM要素/空間の対応表）の仕様とジェネレータ（Epic E9 / E9-2）。

`docs/viewer/digital-twin-spec.md` §4.1 に対応する。ビルOS側のデータモデルには
IFC GUID・空間座標が存在しないため（同 §2）、対応表は本リポジトリ側が自前で
定義・管理する。3つの生成経路（手動記述・IFCプロパティ由来・customTags運用）と、
それらの結果をマージして最終的な `mapping.json` へ組み立てる関数を提供する。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Mapping, Sequence

from .twin import TwinApiError, TwinClient

logger = logging.getLogger(__name__)

_WHITESPACE_RE = re.compile(r"\s+")
_FUZZY_NORMALIZE_RE = re.compile(r"[^A-Z0-9]")


class MappingValidationError(ValueError):
    """`mapping.json`のスキーマ検証に失敗したことを表す。"""


def _normalize_exact(identifier: str) -> str:
    """完全一致判定用の正規化: 大小文字と空白のみを無視する。ハイフン等の区切り文字は
    保持する——区切り文字まで取り除くと位置情報が失われ、"AHU-1-01"と"AHU-10-1"の
    ような別物の識別子が同一視されてしまう（実際に発生を確認した誤結合）。"""
    return _WHITESPACE_RE.sub("", identifier.strip().upper())


def _normalize_fuzzy(identifier: str) -> str:
    """曖昧一致（提案のみ、自動採用しない）判定用の緩い正規化: 記号・空白を含め
    英数字以外を全て取り除く。"""
    return _FUZZY_NORMALIZE_RE.sub("", identifier.upper())


def validate_mapping(data: Mapping) -> None:
    """`mapping.json`（digital-twin-spec.md §4.1）の最低限のスキーマ検証を行う。

    各bindingの`target`は`guid`/`spaceGuid`のどちらか一方のみを持つこと
    （両方・どちらも無い、は不正）。
    """
    if data.get("version") != 1:
        raise MappingValidationError(f"unsupported mapping.json version: {data.get('version')!r}")
    for binding in data.get("bindings", []):
        if "pointId" not in binding or "metric" not in binding:
            raise MappingValidationError(f"binding missing pointId/metric: {binding!r}")
        target = binding.get("target") or {}
        target_keys = {"guid", "spaceGuid"} & target.keys()
        if len(target_keys) != 1:
            raise MappingValidationError(
                f"binding target must have exactly one of guid/spaceGuid, got: {binding!r}"
            )


def load_mapping_json(path: str | Path) -> dict:
    """生成経路1（手動記述）: 人が書いた`mapping.json`を読み込む。検証込み。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    validate_mapping(data)
    return data


def extract_ifc_identifiers(elements: Sequence[Mapping[str, object]], key: str) -> list[dict]:
    """`ifc.get_properties()`の出力列から`key`で指定したプロパティの値を識別子として
    拾う（生成経路2の下ごしらえ）。GlobalIdが無い、または`key`の値が無い/空文字列の
    要素はスキップする（`0`/`False`など文字列以外の非空な値は識別子として採用する）。
    """
    candidates = []
    for props in elements:
        guid = props.get("GlobalId")
        value = props.get(key)
        if not guid or value is None or value == "":
            continue
        candidates.append({"guid": guid, "identifier": str(value)})
    return candidates


def generate_bindings_from_ifc_properties(
    building_os_points: Sequence[Mapping[str, str]],
    ifc_candidates: Sequence[Mapping[str, str]],
) -> dict:
    """生成経路2（IFCプロパティ由来、半自動）。

    `building_os_points`（各`{"pointId", "metric", "identifier"}`）と`ifc_candidates`
    （`extract_ifc_identifiers()`の出力、各`{"guid", "identifier"}`）の識別子を
    正規化して突合する。**一意な完全一致のみ自動採用**し、複数候補一致・部分一致
    （曖昧一致）は`suggestions`（人の確認用の提案リスト）に回すだけで`bindings`
    には入れない。

    Returns:
        `{"bindings": [...], "suggestions": [...], "unmapped": [pointId, ...]}`
    """
    # 正規化結果が空文字列になる識別子（記号のみ等）は候補プールから除外する。
    # 除外しないと、無関係などうし同士が空文字列どうしで「完全一致」してしまう。
    exact_ifc = [
        (c["guid"], _normalize_exact(c["identifier"]))
        for c in ifc_candidates
        if _normalize_exact(c["identifier"])
    ]
    fuzzy_ifc = [
        (c["guid"], _normalize_fuzzy(c["identifier"]))
        for c in ifc_candidates
        if _normalize_fuzzy(c["identifier"])
    ]

    bindings: list[dict] = []
    suggestions: list[dict] = []
    unmapped: list[str] = []

    for point in building_os_points:
        exact_key = _normalize_exact(point["identifier"])
        if not exact_key:
            unmapped.append(point["pointId"])
            continue

        exact = [guid for guid, key in exact_ifc if key == exact_key]
        if len(exact) == 1:
            bindings.append(
                {"pointId": point["pointId"], "metric": point["metric"], "target": {"guid": exact[0]}}
            )
            continue

        fuzzy_key = _normalize_fuzzy(point["identifier"])
        fuzzy = [
            guid
            for guid, key in fuzzy_ifc
            if guid not in exact and (key in fuzzy_key or fuzzy_key in key)
        ]
        candidates = exact + fuzzy
        if candidates:
            suggestions.append(
                {"pointId": point["pointId"], "metric": point["metric"], "candidates": candidates}
            )
        else:
            unmapped.append(point["pointId"])

    return {"bindings": bindings, "suggestions": suggestions, "unmapped": unmapped}


def generate_bindings_from_custom_tags(
    client: TwinClient,
    guid_to_metric: Mapping[str, str],
    tag_prefix: str = "guid:",
) -> dict:
    """生成経路3（ビルOS`customTags`運用）。各GUIDについて
    `customTags=f"{tag_prefix}{guid}"`で逆引きし、一意に見つかった場合のみ採用する
    （0件・複数件は`unmapped`、最も堅牢だがビルOS側データの管理権限が前提）。

    1件のGUID検索が失敗（`TwinApiError`: 上流エラー・到達不能等）しても、それまでに
    解決済みの他のGUIDの結果を破棄せず`unmapped`扱いで継続する。

    Returns:
        `{"bindings": [...], "unmapped": [guid, ...]}`。この`unmapped`はGUIDの列
        であり、生成経路1/2の`unmapped`（pointIdの列）とは意味が異なる点に注意
        —— `build_mapping_json`の`unmapped`（pointId列）へそのまま混ぜず、
        呼び出し側で別枠として扱うこと。
    """
    bindings: list[dict] = []
    unmapped: list[str] = []

    for guid, metric in guid_to_metric.items():
        try:
            results = client.search_resources(custom_tags=f"{tag_prefix}{guid}")
        except TwinApiError:
            logger.warning("customTags lookup failed for GUID %s; treating as unmapped", guid)
            unmapped.append(guid)
            continue
        if len(results) == 1:
            bindings.append({"pointId": results[0]["dtId"], "metric": metric, "target": {"guid": guid}})
        else:
            unmapped.append(guid)

    return {"bindings": bindings, "unmapped": unmapped}


def build_mapping_json(
    bindings: Sequence[Mapping[str, object]],
    unmapped: Sequence[str] = (),
    source: Mapping[str, str] | None = None,
) -> dict:
    """複数の生成経路の結果をマージし、最終的な`mapping.json`（§4.1）を組み立てる。

    `bindings`は`pointId`優先で重複排除する（先に渡された経路が優先——手動記述を
    最初に渡す運用を想定、spec §4.1「併用可、優先度順にマージ」）。`unmapped`は
    重複排除した上で、（別の経路で）結局bindingsに解決できたpointIdを除外する
    ——ある経路の「未解決」が別の経路の成功で上書きされた場合に、同じpointIdが
    bindingsとunmappedの両方に矛盾して現れることを防ぐ。
    """
    seen_point_ids: set[str] = set()
    merged_bindings: list[dict] = []
    for binding in bindings:
        point_id = binding["pointId"]
        if point_id in seen_point_ids:
            continue
        seen_point_ids.add(point_id)
        merged_bindings.append(dict(binding))

    merged_unmapped = [pid for pid in dict.fromkeys(unmapped) if pid not in seen_point_ids]

    data = {
        "version": 1,
        "source": dict(source) if source else {},
        "bindings": merged_bindings,
        "unmapped": merged_unmapped,
    }
    validate_mapping(data)
    return data
