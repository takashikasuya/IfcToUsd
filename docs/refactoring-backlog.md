# バックログ: Epic E10 コード健全化・リファクタリング

2026-08-25 実施のコードベース監査と OpenUSD / IFC 外部動向調査に基づくバックログ。
`docs/viewer/backlog.md` の Epic E1〜E9（機能開発）と番号体系を共有する。

優先度: P1 = 正しさ・堅牢性に関わる（早期に着手） / P2 = 品質・性能・保守性。
規模: S(〜1日) / M(2〜4日) / L(1〜2週)。ID は Epic-Story 形式。

## 調査サマリ（2026-08 時点）

- 依存関係は usd-core 26.5 / ifcopenshell 0.8.5 で最新に近く、**非推奨 USD API の使用はゼロ**
  （25.08〜26.08 の changelog と突き合わせて確認）。
- OpenUSD 26.08 で Python 3.9/3.10 が非推奨化。`requires-python >= 3.10` は将来の
  usd-core 更新と衝突するため引き上げが必要（E10-10）。
- `UsdUtilsComplianceChecker` は削除済み、検証は UsdValidation フレームワークへ移行
  （出力検証の CI 組込は E10-10）。
- AOUSD OpenUSD Core Specification 1.0 公開。本コンバータは `metersPerUnit` を
  設定していない（E10-4）。
- OpenUSD 26.08 でネイティブ LOD スキーマ追加 — `voxelLOD` variant の将来的な移行先候補
  （ウォッチのみ）。
- IFC5 は ECS ベース・JSON・USD 的コンポジションで開発中（段階的リリース予定）。
  ifcopenshell は 0.8.6 リリース済み / 0.9.0 alpha 進行中（いずれもウォッチのみ）。
- 監査での最重要発見: `--y-up` は既知の「0°/180° ヨー限定」に加え、Y/Z スワップが
  行列式 −1 の**鏡像変換**であるため 0° ヨーでも出力が左右反転している（E10-3）。

## Epic E10: コード健全化・リファクタリング

| ID | ストーリー | 優先度 | 規模 | Issue | 受け入れ条件 |
| --- | --- | --- | --- | --- | --- |
| E10-1 | `build_stage` 堅牢化: Site/Building 欠落・複数存在・直下要素で壊れる/無言欠落（`usd.py:150,156,159,172` の `[0]` 参照・裸 assert、取りこぼし検知の追加） | P1 | M | [#58](https://github.com/takashikasuya/IfcToUsd/issues/58) | 縮退モデル（Site 無し/複数 Building/複数分解）の pytest が通り、未配置要素が警告される |
| E10-2 | マテリアル名サニタイズを `Tf.MakeValidIdentifier` + 衝突 dedup に置換（日本語・空白・先頭数字で Sdf パス生成が実行時エラー） | P1 | S | [#59](https://github.com/takashikasuya/IfcToUsd/issues/59) | 敵対的マテリアル名フィクスチャで変換成功・バインド正常 |
| E10-3 | `--y-up` 正規修正: 頂点スワップ全廃、ルート `rotateX=-90` 方式へ（鏡像反転バグの解消。voxel/glTF/space-heatmap も自動的に正しくなる） | P1 | M | [#60](https://github.com/takashikasuya/IfcToUsd/issues/60) | 45° ヨー要素の Y-up ワールド座標が正規軸変換と一致 |
| E10-4 | `metersPerUnit` 設定 + `IfcUnitAssignment` 読み取り（Core Spec 1.0 準拠） | P1 | S | [#61](https://github.com/takashikasuya/IfcToUsd/issues/61) | 出力 `.usda` に `metersPerUnit` が明示され pytest 検証 |
| E10-5 | 構造リファクタ: 行列分解/iterator 定型文/JSON エンベロープの重複排除、`geometries` NamedTuple 化、`Export()`→`Save()` 統一、`specularColor` 死にデータ解消、`"PBRShader"` 定数化、デッドコード削除 | P2 | M | [#62](https://github.com/takashikasuya/IfcToUsd/issues/62) | 既存 pytest 全グリーン（純リファクタ） |
| E10-6 | 性能改善: 頂点変換 numpy 化、flood-fill 配列化、voxelize の 2×LOD 重複ボクセル化解消、convert ピークメモリ削減 | P2 | M | [#63](https://github.com/takashikasuya/IfcToUsd/issues/63) | 出力等価 + ToyodaLab での前後計測を PR に記載 |
| E10-7 | TwinProxy: ポイント fetch 並列化 + per-metric ロック（直列 N×10s ブロッキングと TTL 切れスタンピード対策） | P2 | S | [#64](https://github.com/takashikasuya/IfcToUsd/issues/64) | 既存契約（障害分離/stale/TTL）維持 + 単一リフレッシュ検証テスト |
| E10-8 | 外部入力ガード: 任意 USD の `PBRShader`/`diffuseColor` 前提の除去、プロパティ抽出の例外情報、`Quantities` 全走査、serve の BrokenPipe 等 | P2 | S | [#65](https://github.com/takashikasuya/IfcToUsd/issues/65) | 異なるシェーダ命名の USD で serve/export-gltf が動作 |
| E10-9 | `viewer.js`（2,348 行）のモジュール分割 + 純関数ユニットテスト整備（mortonDecode BigInt 閾値、turbo LUT 等を先に固める） | P2 | L | [#66](https://github.com/takashikasuya/IfcToUsd/issues/66) | Playwright 全グリーン + ユニットテスト CI 実行 |
| E10-10 | 依存・仕様追従: `requires-python >= 3.11`、usd-core 26.x 追従、UsdValidation/usdchecker の CI 組込、IFC5/LOD スキーマのウォッチ | P2 | S | [#67](https://github.com/takashikasuya/IfcToUsd/issues/67) | USD 検証ステップが CI に入りフィクスチャがパス |
| E10-11 | テストカバレッジ補強: `get_properties`/`create_materials` 等の直接ユニットテスト、複数マテリアル shape・`IfcElementQuantity` 等の未踏分岐 | P2 | S | [#68](https://github.com/takashikasuya/IfcToUsd/issues/68) | 対象関数の直接テスト追加・グリーン |

## 実施順序（依存関係)

1. **E10-2, E10-4**（即効・低リスク。E10-4 は E10-10 の検証組込の前提）
2. **E10-1**（堅牢化。E10-11 の縮退モデルテストと同時に）
3. **E10-3**（座標系の正規修正。テスト期待値が動くため単独 PR で）
4. **E10-5**（純リファクタ。E10-3 でスワップ分岐が消えた後が最も薄くなる）
5. **E10-6, E10-7, E10-8, E10-11**(独立して並行可)
6. **E10-9**(最後。ユニットテスト整備 → 段階分割)
7. **E10-10** は独立(いつでも可、ただし E10-4 の後が自然)

## 監査で確認済み・対応不要の項目

- `displayName`/`hidden` メタデータ(25.11 で非推奨)・`UsdUtils` 系・`UsdZipFile` 等の
  削除済み API: いずれも未使用。
- ifcopenshell 0.8 の使用パターン(文字列キー settings、`.r()/.g()/.b()`、
  列優先フラット行列): 現行ドキュメント準拠で健全。
