"""ビルOS（GUTP Building OS RI 等）連携アダプタ層（Epic E9 / E9-1）。

`docs/viewer/digital-twin-spec.md` §2〜§4 に対応する。ビルOSのREST API
（階層走査・最新値・期間クエリ・リソース検索）への薄いラッパー`TwinClient`と、
`serve --twin`が焼き込む静的マニフェスト`twin.json`のスキーマを確定する
`build_twin_json()`を提供する。

読み取り専用。認証はKeycloak JWTのBearerトークンを`token`として渡す想定だが、
OIDCフロー自体（トークン取得）はこのアダプタの範囲外——`--twin twin-config.json`
（E9-3）が発行済みトークンを渡す運用を想定する。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping, Sequence

DEFAULT_TIMEOUT_SECONDS = 10.0


class TwinApiError(RuntimeError):
    """ビルOS APIへのリクエストが失敗したことを表す。

    上流がHTTPエラーで応答した場合、`status_code`にそのステータスコードが入る
    （ネットワーク到達不能・レスポンス本文の解釈失敗の場合は`None`）。呼び出し側
    （E9-3のプロキシ等）が「4xx/5xxの種別に応じた扱い」と「到達不能・応答不正」を
    区別できるようにするための最小限の構造化。
    """

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class TwinClient:
    """ビルOS REST API（読み取り専用）への薄いラッパー。

    階層走査 (`/api/buildings` → `/api/floors` → `/api/spaces` → `/api/devices`
    → `/api/points`)、最新値・期間クエリ (`/telemetries/query`)、リソース検索
    (`/resources/search`) をそれぞれ1メソッドに対応させる。制御API
    （`POST /api/points/<id>/control`等）はここに含めない
    （読み取り専用アダプタという設計方針、digital-twin-spec.md §3）。
    """

    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    def _get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        url = f"{self._base_url}{path}"
        if params:
            query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
            if query:
                url = f"{url}?{query}"

        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body = response.read()
            return json.loads(body.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise TwinApiError(f"GET {url} failed: HTTP {exc.code}", status_code=exc.code) from exc
        except urllib.error.URLError as exc:
            raise TwinApiError(f"GET {url} failed: {exc.reason}") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TwinApiError(f"GET {url} failed: invalid response body ({exc})") from exc

    # --- 階層走査 (digital-twin-spec.md §2) ---

    def list_buildings(self) -> list[dict]:
        return self._get("/api/buildings")

    def list_floors(self, building_dt_id: str) -> list[dict]:
        return self._get("/api/floors", {"buildingDtId": building_dt_id})

    def list_spaces(self, floor_dt_id: str) -> list[dict]:
        return self._get("/api/spaces", {"floorDtId": floor_dt_id})

    def list_devices(self, space_dt_id: str) -> list[dict]:
        return self._get("/api/devices", {"spaceDtId": space_dt_id})

    def list_points(self, device_dt_id: str) -> list[dict]:
        return self._get("/api/points", {"deviceDtId": device_dt_id})

    # --- 計測値 (digital-twin-spec.md §2) ---

    def get_latest(self, point_id: str) -> dict:
        """`{"pointId", "value", "datetime", "unit"}`を返す。"""
        return self._get("/telemetries/query", {"pointId": point_id, "latest": "true"})

    def get_history(
        self,
        point_id: str,
        start: str,
        end: str,
        granularity: str = "None",
    ) -> list[dict]:
        """`[{"datetime", "value"}, ...]`を返す。`granularity`は`None`/`Hour`/`Day`。"""
        return self._get(
            "/telemetries/query",
            {"pointId": point_id, "start": start, "end": end, "granularity": granularity},
        )

    # --- リソース検索 (digital-twin-spec.md §4.1 経路3: customTags運用) ---

    def search_resources(self, q: str | None = None, custom_tags: str | None = None) -> list[dict]:
        return self._get("/resources/search", {"q": q, "customTags": custom_tags})


def build_twin_json(
    metrics: Sequence[Mapping[str, Any]],
    bindings: Sequence[Mapping[str, Any]],
    poll_interval_seconds: int = 10,
    stale_threshold_seconds: int | None = None,
) -> dict:
    """`serve --twin`が焼き込む静的マニフェスト`twin.json`を組み立てる
    （digital-twin-spec.md §4.2）。

    値そのものは含めない（ライブ値は`/api/twin/values`から取得する、E9-3）。
    `stale_threshold_seconds`未指定時は`poll_interval_seconds`の3倍
    （digital-twin-spec.md §5.2: 「ポーリング間隔×3より古い値」）。

    Args:
        metrics: 各メトリックの`{"name", "unit", "min"?, "max"?, "colormap"}`。
            min/max省略時はビューワー側が受信値のP5〜P95から自動決定する
            （digital-twin-spec.md §5.2）。
        bindings: `mapping.json`（E9-2, digital-twin-spec.md §4.1）の`bindings`
            配列をそのまま渡す想定:
            `{"pointId", "metric", "target": {"guid" | "spaceGuid": ...}}`。
            変換を挟まないことで、mapping.jsonのスキーマ変更がこの関数を素通り
            できるようにする（E9-2/E9-3間に別形式への詰め替え層を作らない）。
    """
    return {
        "version": 1,
        "pollIntervalSeconds": poll_interval_seconds,
        "staleThresholdSeconds": (
            stale_threshold_seconds
            if stale_threshold_seconds is not None
            else poll_interval_seconds * 3
        ),
        "metrics": list(metrics),
        "bindings": list(bindings),
    }
