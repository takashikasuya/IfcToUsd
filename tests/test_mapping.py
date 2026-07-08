"""`ifc2usd/mapping.py`（mapping.json層、E9-2）のテスト。

digital-twin-spec.md §4.1 の3生成経路（手動記述・IFCプロパティ由来・customTags運用）
それぞれの単体テストと、曖昧一致が自動採用されないことの検証。
"""

from __future__ import annotations

import json

import pytest

from ifc2usd.mapping import (
    MappingValidationError,
    build_mapping_json,
    extract_ifc_identifiers,
    generate_bindings_from_custom_tags,
    generate_bindings_from_ifc_properties,
    load_mapping_json,
    validate_mapping,
)
from ifc2usd.twin import TwinClient

# --- 生成経路1: 手動記述 ---


def test_load_mapping_json_reads_valid_file(tmp_path):
    path = tmp_path / "mapping.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "source": {"buildingOs": "http://localhost:5000"},
                "bindings": [
                    {"pointId": "point-1", "metric": "temperature", "target": {"guid": "guid-1"}}
                ],
                "unmapped": [],
            }
        ),
        encoding="utf-8",
    )

    data = load_mapping_json(path)
    assert data["bindings"][0]["pointId"] == "point-1"


def test_load_mapping_json_rejects_unsupported_version(tmp_path):
    path = tmp_path / "mapping.json"
    path.write_text(json.dumps({"version": 2, "bindings": []}), encoding="utf-8")

    with pytest.raises(MappingValidationError):
        load_mapping_json(path)


def test_validate_mapping_rejects_binding_without_target():
    with pytest.raises(MappingValidationError):
        validate_mapping(
            {"version": 1, "bindings": [{"pointId": "p1", "metric": "temperature", "target": {}}]}
        )


def test_validate_mapping_rejects_binding_with_both_guid_and_space_guid():
    with pytest.raises(MappingValidationError):
        validate_mapping(
            {
                "version": 1,
                "bindings": [
                    {
                        "pointId": "p1",
                        "metric": "temperature",
                        "target": {"guid": "g1", "spaceGuid": "s1"},
                    }
                ],
            }
        )


def test_validate_mapping_rejects_null_target_without_crashing():
    """`target`が明示的に`null`（手書きmapping.jsonにありうる）でも、生の
    AttributeErrorではなくMappingValidationErrorとして扱う。"""
    with pytest.raises(MappingValidationError):
        validate_mapping(
            {"version": 1, "bindings": [{"pointId": "p1", "metric": "temperature", "target": None}]}
        )


def test_validate_mapping_accepts_space_guid_target():
    validate_mapping(
        {
            "version": 1,
            "bindings": [{"pointId": "p1", "metric": "co2", "target": {"spaceGuid": "s1"}}],
        }
    )


# --- 生成経路2: IFCプロパティ由来 ---


def test_extract_ifc_identifiers_skips_elements_missing_key_or_guid():
    elements = [
        {"GlobalId": "guid-1", "Tag": "SENSOR-001"},
        {"GlobalId": "guid-2", "Tag": ""},
        {"GlobalId": "guid-3"},
        {"Tag": "SENSOR-004"},
    ]

    candidates = extract_ifc_identifiers(elements, key="Tag")

    assert candidates == [{"guid": "guid-1", "identifier": "SENSOR-001"}]


def test_generate_bindings_from_ifc_properties_exact_match_is_auto_adopted():
    points = [{"pointId": "point-1", "metric": "temperature", "identifier": "sensor-001"}]
    ifc_candidates = [{"guid": "guid-1", "identifier": "SENSOR-001"}]

    result = generate_bindings_from_ifc_properties(points, ifc_candidates)

    assert result["bindings"] == [
        {"pointId": "point-1", "metric": "temperature", "target": {"guid": "guid-1"}}
    ]
    assert result["suggestions"] == []
    assert result["unmapped"] == []


def test_generate_bindings_from_ifc_properties_fuzzy_match_is_not_auto_adopted():
    """受け入れ条件: 曖昧一致は自動採用されず、bindingsには入らない。"""
    points = [{"pointId": "point-1", "metric": "temperature", "identifier": "SENSOR-001-TEMP"}]
    ifc_candidates = [{"guid": "guid-1", "identifier": "SENSOR-001"}]

    result = generate_bindings_from_ifc_properties(points, ifc_candidates)

    assert result["bindings"] == []
    assert result["suggestions"] == [
        {"pointId": "point-1", "metric": "temperature", "candidates": ["guid-1"]}
    ]
    assert result["unmapped"] == []


def test_generate_bindings_from_ifc_properties_no_match_goes_to_unmapped():
    points = [{"pointId": "point-1", "metric": "temperature", "identifier": "no-such-sensor"}]
    ifc_candidates = [{"guid": "guid-1", "identifier": "SENSOR-001"}]

    result = generate_bindings_from_ifc_properties(points, ifc_candidates)

    assert result["bindings"] == []
    assert result["suggestions"] == []
    assert result["unmapped"] == ["point-1"]


def test_generate_bindings_from_ifc_properties_multiple_exact_matches_go_to_suggestions():
    """複数のIFC要素が同じ識別子を持つ場合も自動採用しない（あいまいさは人が解消する）。"""
    points = [{"pointId": "point-1", "metric": "temperature", "identifier": "SENSOR-001"}]
    ifc_candidates = [
        {"guid": "guid-1", "identifier": "SENSOR-001"},
        {"guid": "guid-2", "identifier": "sensor-001"},
    ]

    result = generate_bindings_from_ifc_properties(points, ifc_candidates)

    assert result["bindings"] == []
    assert result["suggestions"] == [
        {"pointId": "point-1", "metric": "temperature", "candidates": ["guid-1", "guid-2"]}
    ]


def test_generate_bindings_from_ifc_properties_does_not_collapse_delimiter_position():
    """"AHU-1-01"と"AHU-10-1"のように区切り文字の位置が異なるだけの識別子を
    完全一致と誤判定しない（記号を全て取り除く正規化だとどちらも"AHU101"に
    潰れて誤結合する、実際に発生を確認したバグの回帰）。"""
    points = [{"pointId": "point-1", "metric": "temperature", "identifier": "AHU-1-01"}]
    ifc_candidates = [{"guid": "guid-1", "identifier": "AHU-10-1"}]

    result = generate_bindings_from_ifc_properties(points, ifc_candidates)

    assert result["bindings"] == []


def test_generate_bindings_from_ifc_properties_empty_identifier_is_unmapped_not_matched():
    """識別子が記号のみ等で正規化後に空文字列になる場合、無関係などうしが
    空文字列どうしで「完全一致」してしまわないよう、無条件でunmapped扱いにする。"""
    points = [{"pointId": "point-1", "metric": "temperature", "identifier": "###"}]
    ifc_candidates = [{"guid": "guid-1", "identifier": "---"}]

    result = generate_bindings_from_ifc_properties(points, ifc_candidates)

    assert result["bindings"] == []
    assert result["suggestions"] == []
    assert result["unmapped"] == ["point-1"]


# --- 生成経路3: ビルOS customTags運用 ---


def test_generate_bindings_from_custom_tags_unique_match_is_adopted(mock_twin_server):
    client = TwinClient(mock_twin_server)

    result = generate_bindings_from_custom_tags(client, {"2AeZbGoSL7": "temperature"})

    assert result["bindings"] == [
        {"pointId": "point-temp-1", "metric": "temperature", "target": {"guid": "2AeZbGoSL7"}}
    ]
    assert result["unmapped"] == []


def test_generate_bindings_from_custom_tags_no_match_goes_to_unmapped(mock_twin_server):
    client = TwinClient(mock_twin_server)

    result = generate_bindings_from_custom_tags(client, {"no-such-guid": "temperature"})

    assert result["bindings"] == []
    assert result["unmapped"] == ["no-such-guid"]


def test_generate_bindings_from_custom_tags_multiple_matches_go_to_unmapped(mock_twin_server):
    """一意に決まらない逆引きは自動採用しない。"""
    client = TwinClient(mock_twin_server)

    result = generate_bindings_from_custom_tags(client, {"ambiguous-guid": "temperature"})

    assert result["bindings"] == []
    assert result["unmapped"] == ["ambiguous-guid"]


def test_generate_bindings_from_custom_tags_upstream_error_does_not_lose_other_results(mock_twin_server):
    """1件のGUID検索がTwinApiErrorで失敗しても、他のGUIDの解決結果は失われず、
    失敗した分はunmapped扱いで継続する。"""
    client = TwinClient(mock_twin_server)

    result = generate_bindings_from_custom_tags(
        client, {"trigger-500": "temperature", "2AeZbGoSL7": "temperature"}
    )

    assert result["bindings"] == [
        {"pointId": "point-temp-1", "metric": "temperature", "target": {"guid": "2AeZbGoSL7"}}
    ]
    assert result["unmapped"] == ["trigger-500"]


# --- マージ ---


def test_build_mapping_json_dedups_by_point_id_preferring_first_source():
    manual = [{"pointId": "point-1", "metric": "temperature", "target": {"guid": "guid-1"}}]
    from_ifc_properties = [{"pointId": "point-1", "metric": "temperature", "target": {"guid": "guid-2"}}]

    data = build_mapping_json(manual + from_ifc_properties)

    assert len(data["bindings"]) == 1
    assert data["bindings"][0]["target"]["guid"] == "guid-1"


def test_build_mapping_json_includes_source_and_unmapped():
    data = build_mapping_json(
        [{"pointId": "point-1", "metric": "temperature", "target": {"guid": "guid-1"}}],
        unmapped=["point-2"],
        source={"buildingOs": "http://localhost:5000", "buildingDtId": "building-1"},
    )

    assert data == {
        "version": 1,
        "source": {"buildingOs": "http://localhost:5000", "buildingDtId": "building-1"},
        "bindings": [{"pointId": "point-1", "metric": "temperature", "target": {"guid": "guid-1"}}],
        "unmapped": ["point-2"],
    }


def test_build_mapping_json_validates_output():
    with pytest.raises(MappingValidationError):
        build_mapping_json([{"pointId": "point-1", "metric": "temperature", "target": {}}])


def test_build_mapping_json_drops_unmapped_entries_later_resolved_by_bindings():
    """ある経路ではunmapped扱いだったpointIdが、別の経路のbindingsで解決済みの
    場合、最終結果のunmappedからは除く（bindings/unmappedの両方に矛盾して
    現れることを防ぐ）。"""
    data = build_mapping_json(
        bindings=[{"pointId": "point-1", "metric": "temperature", "target": {"guid": "guid-1"}}],
        unmapped=["point-1", "point-2"],
    )

    assert data["bindings"] == [
        {"pointId": "point-1", "metric": "temperature", "target": {"guid": "guid-1"}}
    ]
    assert data["unmapped"] == ["point-2"]


def test_build_mapping_json_dedups_unmapped():
    data = build_mapping_json([], unmapped=["point-2", "point-2"])
    assert data["unmapped"] == ["point-2"]
