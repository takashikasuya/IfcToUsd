# ビルOS実インスタンス接続 手動確認チェックリスト（E9-1）

`ifc2usd/twin.py`の`TwinClient`を、GUTPビルOS RI（[gutp-bim/gutp-building-os-ri]
(https://github.com/gutp-bim/gutp-building-os-ri)）の実インスタンス（または
`docker-compose.oss.yaml`でローカル起動したインスタンス）に対して手動確認するための
手順。E4-1/E4-2（usdview/Blender/Omniverse確認）の前例に従い、pytestでは
オフラインのモックサーバー（`tests/conftest.py`の`mock_twin_server`）に対して
自動テストする一方、実インスタンス固有の挙動（認証・CORS・実データの形）は
本チェックリストで手動確認する。

> **本チェックリストの実行について**: このサンドボックス環境には`docker-compose.oss.yaml`
> を起動できるDocker環境が無く、外部の実ビルOSインスタンスも用意されていない
> （digital-twin-spec.md §8の未解決事項1）。そのため以下はドキュメント化のみで、
> 実際の実行はDocker環境が使える開発者のローカルマシン等で行う必要がある。

## 前提

- `gutp-bim/gutp-building-os-ri`をclone済み、`docker-compose.oss.yaml`で起動できること
  （.NET 8 / Next.js / NATS JetStream / OxiGraph / TimescaleDB一式）。
- 開発用に`DISABLE_AUTH=true`でKeycloak認証を無効化するか、有効化したままJWTを
  取得できること。

## 1. インスタンス起動と疎通確認

```bash
git clone https://github.com/gutp-bim/gutp-building-os-ri.git
cd gutp-building-os-ri
docker compose -f docker-compose.oss.yaml up -d
curl http://localhost:5000/api/buildings
```

- [ ] `docker compose up`がエラーなく完了し、全サービスが起動する
- [ ] `curl`で`/api/buildings`がJSON配列を返す（空配列でもよい、疎通確認が目的）

## 2. `TwinClient`での階層走査

```bash
uv run python -c "
from ifc2usd.twin import TwinClient
client = TwinClient('http://localhost:5000')
buildings = client.list_buildings()
print(buildings)
for b in buildings:
    floors = client.list_floors(b['dtId'])
    print(' floors:', floors)
"
```

- [ ] `list_buildings()`が実データの建物一覧を返す
- [ ] `list_floors`/`list_spaces`/`list_devices`/`list_points`と辿れ、
      各階層のdtIdが実データのSBCOオントロジー識別子と一致する
- [ ] レスポンス形が`digital-twin-spec.md` §2記載の想定と一致する
      （モックサーバーが返す形と実データの形にズレが無いか確認。ズレがあれば
      `twin.py`のパース処理・モックサーバー双方を実データに合わせて修正すること）

## 3. 最新値・期間クエリ

```bash
uv run python -c "
from ifc2usd.twin import TwinClient
client = TwinClient('http://localhost:5000')
# 実データのpointIdに置き換えて実行
print(client.get_latest('<point-id>'))
print(client.get_history('<point-id>', start='2026-07-01T00:00:00Z', end='2026-07-08T00:00:00Z', granularity='Hour'))
"
```

- [ ] `get_latest`が`{"pointId", "value", "datetime", "unit"}`形のデータを返す
- [ ] `get_history`が`[{"datetime", "value"}, ...]`形のデータを返す

## 4. CORS/認証の実挙動確認（digital-twin-spec.md §8 未解決事項2）

- [ ] ブラウザの開発者ツールから直接`http://localhost:5000/api/buildings`へ
      `fetch()`し、CORSエラーになるか（`CORS_ALLOWED_ORIGINS`環境変数の実際の設定を確認）
- [ ] 認証が有効な場合、Keycloakからトークンを取得し`TwinClient(base_url, token=...)`
      で`Authorization: Bearer`ヘッダが通ることを確認
- [ ] 上記に関わらず、E9-3のプロキシ設計（サーバー側で中継しトークンをブラウザへ
      出さない）は変更しない（直接fetchは将来の最適化オプションとして記録するのみ）

## 5. 実データ固有の制約確認（digital-twin-spec.md §8 未解決事項3・4）

- [ ] 対象建物データに`sbco:Room`ノードが存在するか（無ければE9-5のフロア単位
      フォールバックが必須になる）
- [ ] センサー機器のIFC対応（Pset由来の機器タグ・型番）が実データに存在するか
      （無ければE9-2はデモ用の合成`mapping.json`+モック値で機能実証する）

## 結果の記録

実施日・ビルOSインスタンスのバージョン/コミットハッシュ・各チェック項目の
PASS/FAIL・実データとモックのペイロード形の差分（あれば）を本ファイルに追記するか、
実施記録として別途保存すること。
