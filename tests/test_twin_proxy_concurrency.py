"""TwinProxy の並列取得と per-metric ロック（E10-7 / Issue #64）の検証。

数千ポイント規模では直列 fetch が `N × タイムアウト` のブロッキングになる。
また TTL 失効の瞬間に複数リクエストが重なると同じメトリックを同時に取りに行く
（キャッシュスタンピード）。並列化とメトリック単位のロックで両方を抑える。
"""

from __future__ import annotations

import threading
import time

import pytest

from ifc2usd.twin_proxy import TwinProxy

BINDINGS = [
    {"pointId": f"point-{i}", "metric": "temperature", "target": {"guid": f"guid-{i}"}}
    for i in range(8)
]


class _SlowClient:
    """1 ポイントあたり一定時間かかる上流。並列度を同時実行数として観測する。"""

    def __init__(self, delay: float = 0.05) -> None:
        self.delay = delay
        self.calls = 0
        self.max_concurrent = 0
        self._active = 0
        self._lock = threading.Lock()

    def get_latest(self, point_id: str) -> dict:
        with self._lock:
            self.calls += 1
            self._active += 1
            self.max_concurrent = max(self.max_concurrent, self._active)
        try:
            time.sleep(self.delay)
        finally:
            with self._lock:
                self._active -= 1
        return {"pointId": point_id, "value": 1.0, "unit": "u", "datetime": "t0"}


def test_points_are_fetched_concurrently():
    client = _SlowClient()
    proxy = TwinProxy(client, BINDINGS, ttl_seconds=10)

    result = proxy.get_values("temperature")

    assert len(result["values"]) == len(BINDINGS)
    assert client.max_concurrent > 1


def test_values_keep_binding_order():
    proxy = TwinProxy(_SlowClient(delay=0.0), BINDINGS, ttl_seconds=10)

    values = proxy.get_values("temperature")["values"]

    assert [v["pointId"] for v in values] == [b["pointId"] for b in BINDINGS]


def test_concurrent_callers_trigger_a_single_refresh():
    client = _SlowClient(delay=0.1)
    proxy = TwinProxy(client, BINDINGS, ttl_seconds=10)

    threads = [threading.Thread(target=proxy.get_values, args=("temperature",)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 4 スレッドが同時に来ても上流呼び出しは 1 回分（=ポイント数）に収まる
    assert client.calls == len(BINDINGS)


def test_a_slow_metric_does_not_block_another_metric():
    """ロックはメトリック単位。別メトリックの取得が待たされない。"""
    bindings = BINDINGS + [{"pointId": "co2-1", "metric": "co2", "target": {"guid": "g"}}]
    client = _SlowClient(delay=0.2)
    proxy = TwinProxy(client, bindings, ttl_seconds=10)

    slow = threading.Thread(target=proxy.get_values, args=("temperature",))
    slow.start()
    time.sleep(0.02)

    started = time.monotonic()
    proxy.get_values("co2")
    elapsed = time.monotonic() - started
    slow.join()

    assert elapsed < 0.2 * len(BINDINGS)


def test_per_point_failures_are_still_isolated():
    class _PartlyFlaky(_SlowClient):
        def get_latest(self, point_id: str) -> dict:
            if point_id == "point-3":
                from ifc2usd.twin import TwinApiError

                raise TwinApiError("boom")
            return super().get_latest(point_id)

    proxy = TwinProxy(_PartlyFlaky(delay=0.0), BINDINGS, ttl_seconds=10)

    values = proxy.get_values("temperature")["values"]

    assert len(values) == len(BINDINGS) - 1
    assert "point-3" not in [v["pointId"] for v in values]
