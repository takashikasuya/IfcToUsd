# Epic E9 実装計画（ビルOS連携デジタルツイン表示）

`docs/viewer/backlog.md` の Epic E9 と `docs/viewer/digital-twin-spec.md` を元にした、
着手前の実装計画。Epic E5/E6 の前例（着手時にストーリー単位の個別Issueへ分割する）に
従い、E9-1〜E9-6 をそれぞれ独立Issueとして親Issue #41 にぶら下げてから着手する。

## 前提確認（2026-07-08時点）

- Epic E8（ビューワーUX改良）は全ストーリー完了済み（コミット履歴・CLAUDE.md記載を確認）。
  E9-4/E9-5 が依存する E8-6（ボクセル真っ黒バグ修正）・E8-1（選択ハイライトのマテリアル
  クローン戦略）は満たされている。
- E9関連のコードは未着手。`ifc2usd/` に `twin.py` やマッピング関連モジュールは存在せず、
  `ifc2usd/viewer/viewer.js:335` に Live Data セクションの空プレースホルダがあるのみ
  （`tests/test_property_panel_e2e.py::test_live_data_placeholder_exists_when_element_selected`
  が検証対象）。
- 親Issue #41（Epic E9）が存在。Issue #30（E5-4: センサー時系列の空間集計表示）は
  E9-5が実現してクローズする対象としてbacklogに明記されている。
- ビルOS側の最重要制約（仕様書2節で一次ソース確認済み）: **GUID/空間座標がビルOS側
  データモデルに存在しない**。GUID⇔pointIdのマッピングは本リポジトリが自前で持つ
  （`mapping.json`）。CORS設定は未確認のため、サーバー側プロキシを基本設計とする。

## ストーリー別実装計画

依存関係は直列: E9-1 → E9-2 → E9-3 → E9-4 → E9-5 → E9-6
（各ストーリーは前段の成果物を前提に積み上げる）。

### E9-1: ビルOS API接続PoC＋アダプタ層設計確定

- 成果物: `ifc2usd/twin.py`（APIクライアント骨格：階層走査・最新値・期間クエリの
  薄いラッパー）、pytest用の**モックHTTPサーバーフィクスチャ**（仕様書2節記載の
  ペイロード形を返す）、`twin.json`スキーマの確定。
- テスト: モックサーバーに対するアダプタ単体テスト。実インスタンス接続は
  E4-1/E4-2の前例に従い手動確認チェックリスト化（`docs/viewer/`配下に追加）。
- 未解決事項（着手時に確定）: 実インスタンスの有無（無ければ`docker-compose.oss.yaml`
  ローカル起動 or モックのみで進行）、CORS/認証の実挙動。

### E9-2: マッピング層（mapping.json）の仕様とジェネレータ

- 成果物: `mapping.json`ローダー、3経路の生成器
  1. 手動記述（正攻法、そのままロード）
  2. IFCプロパティ由来（`ifc.py`の`get_properties()`が返すPsetと、ビルOS側識別子の
     文字列規約一致で候補生成。曖昧一致は自動採用せず提案リストのみ）
  3. ビルOS`customTags`運用（IFC GUIDをビルOS側に登録し`/resources/search?customTags=`
     で逆引き）
- 未マッピング点は`unmapped`配列として警告出力。
- テスト: 3経路それぞれの単体テスト、曖昧一致が自動採用されないことの検証。

### E9-3: serve拡張（--twinモード）

- 成果物: `GET /api/twin/values?metric=`・`GET /api/twin/history?...`のプロキシ
  エンドポイント（ホワイトリスト方式、制御APIは中継しない）、メトリックごとの
  TTLキャッシュ、上流エラー時のstale応答（最後の成功値+`stale: true`）、
  `twin.json`の静的焼き込み（`build_serve_directory(..., twin=...)`）。
  トークン/クレデンシャルは`--twin twin-config.json`のみに存在し`twin.json`には含めない。
- テスト: プロキシのモックHTTPテスト、TTLキャッシュ境界、上流エラー時のstale応答。

### E9-4: オブジェクト表示（値の色マッピング＋凡例＋Live Dataパネル）

- 成果物: turbo系256エントリLUTによるカラーマップ、対象GUIDメッシュへの適用
  （E8-1のマテリアルクローン戦略を踏襲し共有マテリアルへの波及を防ぐ）、
  stale値の彩度低下表示、画面隅の凡例（min/max/単位）、プロパティパネルの
  Live Dataセクション（メトリック名・最新値・単位・取得時刻＋canvas直描き
  スパークライン、外部チャートライブラリ不使用）。
- ツールバーに「Live」グループを追加（E8-5のグループ化規約に従う）。
  `scene.json`に`assets.twin`が無ければグループ自体を出さない
  （SDFスライスと同じ付加的アセット規約）。
- テスト: Playwright画素検証（E8-6/E8-1の前例踏襲）、stale彩度低下の検証。

### E9-5: 空間/ボクセルヒートマップ（Issue #30クローズ）

- **先行タスク**: 現行`ifc.py`の`get_geometry()`はIfcSpaceジオメトリを除外している
  ため、空間専用の抽出経路（`get_space_geometry()`等）を追加する。
- アルゴリズム: 各IfcSpaceを`voxelize_mesh(..., fill=True)`で充填ボクセル化
  （シーン共有origin・LODサイズはvoxels.jsonと同一規約）→ ボクセル→spaceGuid
  対応表を作成（重複セルは体積の小さい空間を優先）→ `mapping.json`の
  `spaceGuid`バインディングで空間ごとに値を集計（既定は平均、min/max/countも選択可）
  → 集計値の色でInstancedMesh描画（既存のボクセル描画・LOD切替に乗る）。
- 空間ジオメトリが取れないモデルではStorey（フロア）単位のフォールバック集計を提供。
- テスト: 空間ジオメトリ抽出の単体テスト、集計ロジックの単体テスト、
  フォールバックのE2E。

### E9-6: 時系列再生

- 成果物: 期間+粒度指定で`/api/twin/history`を全対象ポイントぶん一括取得し
  `Float32Array`のフレーム列（時刻×ポイント）へ整形、タイムスライダーで再生
  （再生中の逐次fetchはしない）。色適用関数はE9-4と共通化し、ライブ/再生で
  表示経路を分岐させない。
- テスト: フレーム整形の単体テスト、スライダー操作のPlaywright。

## Issue化する際の粒度

backlog.md表の優先度・規模・受け入れ条件をそのまま各Issueの本文に転記し、
E9-1〜E9-6の6件を親Issue #41にぶら下げる（E5/E6の前例と同じ構成）。
本セッションではgithub MCPが未認証のためIssue作成は行わず、本ドキュメントを
次回セッションでのIssue起票・実装着手のベースとする。

## 着手前に確定させること（仕様書8節より）

1. 接続先の実インスタンス有無（無ければ`docker-compose.oss.yaml`ローカル起動 or モックのみ）
2. CORS/認証の実挙動（プロキシ設計自体はこの結果に関わらず維持し、直接fetchは
   将来の最適化オプションとする）
3. マッピング規約（Pset名・機器番号の命名規則）は対象建物データ次第。
   ToyodaLab.ifcにセンサ機器のPsetが無い可能性が高く、その場合はデモ用の
   合成`mapping.json`+モック値で機能を成立させる
4. `sbco:Room`が実データに無い場合のフロア単位フォールバックの扱い
