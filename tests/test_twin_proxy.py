"""`ifc2usd/twin_proxy.py`（ビルOSプロキシ本体、E9-3）のテスト。

TTLキャッシュ境界・上流エラー時のstale応答を、`mock_twin_server`フィクスチャに
対する実際のHTTPラウンドトリップと、注入可能な擬似クロックで検証する。
"""

from __future__ import annotations

import json

import pytest

from ifc2usd.twin import TwinApiError, TwinClient
from ifc2usd.twin_proxy import TwinProxy, load_twin_config


class _FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class _CountingClient(TwinClient):
    """upstreamへの実呼び出し回数を数えるための、テスト専用のスパイ。"""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.get_latest_calls = 0

    def get_latest(self, point_id: str) -> dict:
        self.get_latest_calls += 1
        return super().get_latest(point_id)


BINDINGS = [
    {"pointId": "point-temp-1", "metric": "temperature", "target": {"guid": "2AeZbGoSL7"}},
    {"pointId": "point-co2-1", "metric": "co2", "target": {"spaceGuid": "1xYz"}},
]


def test_get_values_returns_bound_points_for_metric(mock_twin_server):
    client = TwinClient(mock_twin_server)
    proxy = TwinProxy(client, BINDINGS, ttl_seconds=10, now=_FakeClock())

    result = proxy.get_values("temperature")

    assert result == {
        "metric": "temperature",
        "stale": False,
        "values": [
            {
                "pointId": "point-temp-1",
                "value": 23.4,
                "unit": "celsius",
                "datetime": "2026-07-08T09:00:00Z",
                "guid": "2AeZbGoSL7",
            }
        ],
    }


def test_get_values_for_metric_with_no_bound_points_is_empty_not_an_error(mock_twin_server):
    client = TwinClient(mock_twin_server)
    proxy = TwinProxy(client, BINDINGS, ttl_seconds=10, now=_FakeClock())

    result = proxy.get_values("no-such-metric")

    assert result == {"metric": "no-such-metric", "stale": False, "values": []}


def test_get_values_uses_space_guid_target(mock_twin_server):
    client = TwinClient(mock_twin_server)
    proxy = TwinProxy(client, BINDINGS, ttl_seconds=10, now=_FakeClock())

    result = proxy.get_values("co2")

    assert result["values"] == [
        {
            "pointId": "point-co2-1",
            "value": 512,
            "unit": "ppm",
            "datetime": "2026-07-08T09:00:00Z",
            "spaceGuid": "1xYz",
        }
    ]


def test_get_values_within_ttl_does_not_refetch(mock_twin_server):
    client = _CountingClient(mock_twin_server)
    clock = _FakeClock()
    proxy = TwinProxy(client, BINDINGS, ttl_seconds=10, now=clock)

    proxy.get_values("temperature")
    clock.advance(5)  # < TTL
    result = proxy.get_values("temperature")

    assert client.get_latest_calls == 1
    assert result["stale"] is False


def test_get_values_refetches_after_ttl_expires(mock_twin_server):
    client = _CountingClient(mock_twin_server)
    clock = _FakeClock()
    proxy = TwinProxy(client, BINDINGS, ttl_seconds=10, now=clock)

    proxy.get_values("temperature")
    clock.advance(11)  # > TTL
    proxy.get_values("temperature")

    assert client.get_latest_calls == 2


def test_get_values_falls_back_to_stale_cache_on_upstream_error():
    class _FlakyClient:
        def __init__(self) -> None:
            self.calls = 0

        def get_latest(self, point_id: str) -> dict:
            self.calls += 1
            if self.calls == 1:
                return {"pointId": point_id, "value": 1.0, "unit": "u", "datetime": "t0"}
            raise TwinApiError("upstream down")

    clock = _FakeClock()
    proxy = TwinProxy(_FlakyClient(), BINDINGS[:1], ttl_seconds=10, now=clock)

    fresh = proxy.get_values("temperature")
    clock.advance(11)  # force a refetch attempt, which will fail
    stale = proxy.get_values("temperature")

    assert fresh["stale"] is False
    assert stale["stale"] is True
    assert stale["values"] == fresh["values"]


def test_get_values_isolates_per_point_failures():
    """複数ポイントを束ねるメトリックで、1点だけ失敗しても他の点の成功結果は
    失われない（数千ポイント規模で1点の不調が集計全体を巻き込まないため、
    digital-twin-spec.md §6）。"""

    class _PartialFailClient:
        def get_latest(self, point_id: str) -> dict:
            if point_id == "point-b":
                raise TwinApiError("boom")
            return {"pointId": point_id, "value": 1.0, "unit": "u", "datetime": "t0"}

    bindings = [
        {"pointId": "point-a", "metric": "temperature", "target": {"guid": "g1"}},
        {"pointId": "point-b", "metric": "temperature", "target": {"guid": "g2"}},
    ]
    proxy = TwinProxy(_PartialFailClient(), bindings, ttl_seconds=10, now=_FakeClock())

    result = proxy.get_values("temperature")

    assert result["stale"] is False
    assert result["values"] == [
        {"pointId": "point-a", "value": 1.0, "unit": "u", "datetime": "t0", "guid": "g1"}
    ]


def test_get_values_for_unknown_metric_does_not_grow_cache(mock_twin_server):
    """`mapping.json`に無い任意のメトリック名を投げられ続けても、キャッシュ
    エントリを作らない（無制限なメモリ増加を防ぐ）。"""
    client = TwinClient(mock_twin_server)
    proxy = TwinProxy(client, BINDINGS, ttl_seconds=10, now=_FakeClock())

    for i in range(5):
        proxy.get_values(f"no-such-metric-{i}")

    assert proxy._cache == {}


def test_get_values_without_cache_propagates_error_on_upstream_failure():
    class _AlwaysFailsClient:
        def get_latest(self, point_id: str) -> dict:
            raise TwinApiError("upstream down")

    proxy = TwinProxy(_AlwaysFailsClient(), BINDINGS[:1], ttl_seconds=10, now=_FakeClock())

    with pytest.raises(TwinApiError):
        proxy.get_values("temperature")


def test_get_history_passes_through_without_caching(mock_twin_server):
    client = TwinClient(mock_twin_server)
    proxy = TwinProxy(client, BINDINGS, ttl_seconds=10, now=_FakeClock())

    history = proxy.get_history("point-temp-1", start="2026-07-08T00:00:00Z", end="2026-07-08T10:00:00Z")

    assert history == [
        {"datetime": "2026-07-08T08:00:00Z", "value": 22.9},
        {"datetime": "2026-07-08T09:00:00Z", "value": 23.4},
    ]


def test_load_twin_config_resolves_mapping_relative_to_config_file(tmp_path):
    mapping_path = tmp_path / "mapping.json"
    mapping_path.write_text(
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
    config_path = tmp_path / "twin-config.json"
    config_path.write_text(
        json.dumps(
            {
                "buildingOs": {"baseUrl": "http://localhost:5000", "token": "secret-jwt"},
                "mapping": "mapping.json",
                "metrics": [{"name": "temperature", "unit": "celsius", "colormap": "turbo"}],
                "pollIntervalSeconds": 15,
            }
        ),
        encoding="utf-8",
    )

    config = load_twin_config(config_path)

    assert config["base_url"] == "http://localhost:5000"
    assert config["token"] == "secret-jwt"
    assert config["poll_interval_seconds"] == 15
    assert config["stale_threshold_seconds"] is None
    assert config["bindings"] == [
        {"pointId": "point-1", "metric": "temperature", "target": {"guid": "guid-1"}}
    ]


def test_load_twin_config_defaults_poll_interval(tmp_path):
    mapping_path = tmp_path / "mapping.json"
    mapping_path.write_text(json.dumps({"version": 1, "bindings": [], "unmapped": []}), encoding="utf-8")
    config_path = tmp_path / "twin-config.json"
    config_path.write_text(
        json.dumps({"buildingOs": {"baseUrl": "http://localhost:5000"}, "mapping": "mapping.json"}),
        encoding="utf-8",
    )

    config = load_twin_config(config_path)

    assert config["poll_interval_seconds"] == 10
    assert config["token"] is None
