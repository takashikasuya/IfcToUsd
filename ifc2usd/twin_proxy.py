"""ビルOSプロキシ本体（`ifc2usd serve --twin`）: メトリック単位TTLキャッシュと
上流エラー時のstale応答（Epic E9 / E9-3）。

`docs/viewer/digital-twin-spec.md` §3, §4.3 に対応する。制御API
（`POST /api/points/<id>/control`等）は一切扱わない——ホワイトリスト方式で
`get_values`（最新値の束、メトリック単位TTLキャッシュ+staleフォールバック）・
`get_history`（期間指定、キャッシュなし）のみを提供する。
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from .mapping import load_mapping_json
from .twin import TwinApiError, TwinClient

# 1 メトリックのリフレッシュで上流へ同時に投げる最大リクエスト数
_MAX_FETCH_WORKERS = 8


def load_twin_config(path: str | Path) -> dict:
    """`--twin twin-config.json`を読み込み、ビルOS接続情報・メトリック定義・
    `mapping.json`（同ファイルからの相対パス）を解決した辞書を返す。

    トークン/クレデンシャルはこの設定ファイルにのみ存在し、ブラウザへ渡る
    静的マニフェスト`twin.json`には含めない（digital-twin-spec.md §6）。
    """
    config_path = Path(path)
    config = json.loads(config_path.read_text(encoding="utf-8"))

    mapping_path = config_path.parent / config["mapping"]
    mapping = load_mapping_json(mapping_path)

    return {
        "base_url": config["buildingOs"]["baseUrl"],
        "token": config["buildingOs"].get("token"),
        "metrics": config.get("metrics", []),
        "poll_interval_seconds": config.get("pollIntervalSeconds", 10),
        "stale_threshold_seconds": config.get("staleThresholdSeconds"),
        "bindings": mapping["bindings"],
        "source": mapping.get("source", {}),
    }


class TwinProxy:
    """`/api/twin/*`のプロキシ本体。`serve`のHTTPハンドラから呼ばれる。"""

    def __init__(
        self,
        client: TwinClient,
        bindings: Sequence[Mapping[str, object]],
        ttl_seconds: float,
        now: Callable[[], float] = time.monotonic,
        max_workers: int = _MAX_FETCH_WORKERS,
    ) -> None:
        self._client = client
        self._ttl_seconds = ttl_seconds
        self._now = now
        self._max_workers = max_workers
        self._points_by_metric: dict[str, list[Mapping[str, object]]] = {}
        for binding in bindings:
            self._points_by_metric.setdefault(binding["metric"], []).append(binding)
        self._cache: dict[str, tuple[float, list[dict]]] = {}
        self._cache_lock = threading.Lock()
        self._metric_locks: dict[str, threading.Lock] = {}

    def get_values(self, metric: str) -> dict:
        """`{"metric", "stale", "values": [{"pointId","value","unit","datetime","guid"?,"spaceGuid"?}, ...]}`。

        `mapping.json`に無い（=`twin.json`が公開していない）メトリック名は
        キャッシュに触れず即座に空`values`を返す——任意の文字列を投げ続けられて
        `_cache`が無制限に肥大化するのを防ぐ。

        TTL内はキャッシュを返す（`stale=False`）。上流エラーは対象ポイント単位で
        隔離する: 一部のポイントだけ失敗した場合、成功した分だけを返す
        （数千ポイント規模ではポイント1点の不調が集計全体を巻き込むべきではない、
        digital-twin-spec.md §6）。**全ポイントが失敗**した場合のみ、キャッシュが
        あれば最後の成功値を`stale=True`で返し、キャッシュも無ければ最後に
        発生した`TwinApiError`をそのまま送出する（呼び出し側=HTTPハンドラが
        502等へ変換する）。

        ポイントの取得は並列に行う（直列だと N 点 × タイムアウトのブロッキングに
        なる）。また TTL 失効時に複数リクエストが重なっても、上流へのリフレッシュは
        メトリック単位で1回にまとめる（キャッシュスタンピード対策、Issue #64）。
        """
        if metric not in self._points_by_metric:
            return {"metric": metric, "stale": False, "values": []}

        fresh = self._fresh_cached_values(metric)
        if fresh is not None:
            return {"metric": metric, "stale": False, "values": fresh}

        with self._lock_for(metric):
            # ロック待ちの間に別スレッドが更新を終えていることがある
            fresh = self._fresh_cached_values(metric)
            if fresh is not None:
                return {"metric": metric, "stale": False, "values": fresh}
            return self._refresh(metric)

    def _lock_for(self, metric: str) -> threading.Lock:
        with self._cache_lock:
            lock = self._metric_locks.get(metric)
            if lock is None:
                lock = self._metric_locks[metric] = threading.Lock()
            return lock

    def _fresh_cached_values(self, metric: str) -> Optional[list[dict]]:
        with self._cache_lock:
            cached = self._cache.get(metric)
        if cached is not None and (self._now() - cached[0]) < self._ttl_seconds:
            return cached[1]
        return None

    def _refresh(self, metric: str) -> dict:
        bindings = self._points_by_metric[metric]
        values: list[dict] = []
        last_error: TwinApiError | None = None

        with ThreadPoolExecutor(max_workers=min(self._max_workers, len(bindings))) as pool:
            for entry, error in pool.map(self._fetch_one_or_error, bindings):
                if entry is not None:
                    values.append(entry)
                else:
                    last_error = error

        if not values and last_error is not None:
            with self._cache_lock:
                cached = self._cache.get(metric)
            if cached is not None:
                return {"metric": metric, "stale": True, "values": cached[1]}
            raise last_error

        with self._cache_lock:
            self._cache[metric] = (self._now(), values)
        return {"metric": metric, "stale": False, "values": values}

    def _fetch_one_or_error(self, binding: Mapping[str, object]):
        try:
            return self._fetch_one(binding), None
        except TwinApiError as exc:
            return None, exc

    def _fetch_one(self, binding: Mapping[str, object]) -> dict:
        latest = self._client.get_latest(binding["pointId"])
        entry = {
            "pointId": binding["pointId"],
            "value": latest["value"],
            "unit": latest.get("unit"),
            "datetime": latest["datetime"],
        }
        target = binding.get("target", {})
        if "guid" in target:
            entry["guid"] = target["guid"]
        if "spaceGuid" in target:
            entry["spaceGuid"] = target["spaceGuid"]
        return entry

    def get_history(self, point_id: str, start: str, end: str, granularity: str = "None") -> list[dict]:
        """時系列再生用（E9-6）。キャッシュせず上流へそのまま中継する。"""
        return self._client.get_history(point_id, start=start, end=end, granularity=granularity)
