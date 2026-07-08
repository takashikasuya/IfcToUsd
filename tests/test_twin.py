"""`ifc2usd/twin.py`（ビルOSアダプタ層、E9-1）のテスト。

`tests/conftest.py`の`mock_twin_server`フィクスチャが
`docs/viewer/digital-twin-spec.md` §2のペイロード形を返すため、実インスタンス・
docker-compose.oss.yamlに依存せずオフラインで検証できる。
"""

from __future__ import annotations

import socket

import pytest

from conftest import get_last_twin_authorization_header
from ifc2usd.twin import TwinApiError, TwinClient, build_twin_json


def test_list_buildings(mock_twin_server):
    client = TwinClient(mock_twin_server)
    buildings = client.list_buildings()
    assert buildings == [{"dtId": "building-1", "name": "Test Building"}]


def test_hierarchy_traversal_walks_buildings_to_points(mock_twin_server):
    """digital-twin-spec.md §2: buildings→floors→spaces→devices→pointsの階層走査。"""
    client = TwinClient(mock_twin_server)

    [building] = client.list_buildings()
    [floor] = client.list_floors(building["dtId"])
    [space] = client.list_spaces(floor["dtId"])
    [device] = client.list_devices(space["dtId"])
    points = client.list_points(device["dtId"])

    assert floor["dtId"] == "floor-1"
    assert space["dtId"] == "space-1"
    assert device["dtId"] == "device-1"
    assert {p["dtId"] for p in points} == {"point-temp-1", "point-co2-1"}


def test_list_floors_for_unknown_building_returns_empty(mock_twin_server):
    client = TwinClient(mock_twin_server)
    assert client.list_floors("no-such-building") == []


def test_get_latest_returns_documented_shape(mock_twin_server):
    client = TwinClient(mock_twin_server)
    latest = client.get_latest("point-temp-1")
    assert latest == {
        "pointId": "point-temp-1",
        "value": 23.4,
        "datetime": "2026-07-08T09:00:00Z",
        "unit": "celsius",
    }


def test_get_history_returns_documented_shape(mock_twin_server):
    client = TwinClient(mock_twin_server)
    history = client.get_history("point-temp-1", start="2026-07-08T00:00:00Z", end="2026-07-08T10:00:00Z")
    assert history == [
        {"datetime": "2026-07-08T08:00:00Z", "value": 22.9},
        {"datetime": "2026-07-08T09:00:00Z", "value": 23.4},
    ]


def test_get_latest_for_unknown_point_raises_twin_api_error(mock_twin_server):
    client = TwinClient(mock_twin_server)
    with pytest.raises(TwinApiError):
        client.get_latest("no-such-point")


def test_upstream_server_error_raises_twin_api_error(mock_twin_server):
    client = TwinClient(mock_twin_server)
    with pytest.raises(TwinApiError) as excinfo:
        client.get_latest("trigger-500")
    assert excinfo.value.status_code == 500


def test_upstream_404_sets_status_code(mock_twin_server):
    client = TwinClient(mock_twin_server)
    with pytest.raises(TwinApiError) as excinfo:
        client.get_latest("no-such-point")
    assert excinfo.value.status_code == 404


def test_connection_failure_raises_twin_api_error():
    """上流が到達不能な場合もTwinApiErrorに揃える（urllib.error.URLErrorを生で
    外へ漏らさない）。ポートをbindしてすぐ閉じることで、環境依存の特権ポート挙動に
    頼らず確実かつ高速にECONNREFUSEDを起こす。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    client = TwinClient(f"http://127.0.0.1:{port}", timeout=1.0)
    with pytest.raises(TwinApiError) as excinfo:
        client.list_buildings()
    assert excinfo.value.status_code is None


def test_search_resources_by_custom_tags(mock_twin_server):
    """digital-twin-spec.md §4.1 経路3: customTags運用によるGUID逆引き検索。"""
    client = TwinClient(mock_twin_server)
    results = client.search_resources(custom_tags="guid:2AeZbGoSL7")
    assert results == [{"dtId": "point-temp-1", "customTags": ["guid:2AeZbGoSL7"]}]


def test_search_resources_by_query_text(mock_twin_server):
    """digital-twin-spec.md §4.1: `q=`によるテキスト検索経路（customTags指定が無い場合）。"""
    client = TwinClient(mock_twin_server)
    results = client.search_resources(q="temperature")
    assert results == [{"dtId": "point-temp-1", "name": "Temperature"}]


def test_token_is_sent_as_bearer_authorization_header(mock_twin_server):
    client = TwinClient(mock_twin_server, token="secret-jwt")
    client.list_buildings()
    assert get_last_twin_authorization_header() == "Bearer secret-jwt"


def test_no_token_omits_authorization_header(mock_twin_server):
    client = TwinClient(mock_twin_server)
    client.list_buildings()
    assert get_last_twin_authorization_header() is None


def test_build_twin_json_schema():
    """digital-twin-spec.md §4.2: メトリック一覧・マッピング・ポーリング間隔・
    stale閾値を持ち、値そのものは含めない（この等価比較自体が、余計な"value"/"values"
    キーが紛れ込んでいないことも保証する）。bindingsはmapping.json（§4.1）の
    `target`入れ子形をそのまま渡せる。"""
    metrics = [{"name": "temperature", "unit": "celsius", "colormap": "turbo"}]
    bindings = [
        {"pointId": "point-temp-1", "metric": "temperature", "target": {"guid": "2AeZbGoSL7"}}
    ]

    twin_json = build_twin_json(metrics, bindings, poll_interval_seconds=10)

    assert twin_json == {
        "version": 1,
        "pollIntervalSeconds": 10,
        "staleThresholdSeconds": 30,
        "metrics": metrics,
        "bindings": bindings,
    }


def test_build_twin_json_stale_threshold_is_explicit_when_given():
    twin_json = build_twin_json([], [], poll_interval_seconds=10, stale_threshold_seconds=120)
    assert twin_json["staleThresholdSeconds"] == 120
