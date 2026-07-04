# ビューワー構想ドキュメント

USD（`ifc2usd` の出力）と、本リポジトリのボクセル表現（`GLTF_to_Voxel.ipynb` が生成する
セマンティックボクセル JSON）を統合的に閲覧するビューワーの検討資料。

| ドキュメント | 内容 |
| --- | --- |
| [research.md](research.md) | 調査結果: リポジトリ内資産の棚卸し、Hydra アーキテクチャ、USD ビューワーの選択肢、ボクセル表示技術 |
| [architecture.md](architecture.md) | アーキテクチャ: Hydra に倣った層分離、2系統の表示経路（USDネイティブ / Web）、全体図 |
| [spec.md](spec.md) | 仕様: CLI、ボクセル JSON v2 スキーマ、USD オーサリング規約、ビューワー機能要件・非機能要件 |
| [backlog.md](backlog.md) | バックログ: エピック / ストーリー、優先度、受け入れ条件 |
| [usdview-checklist.md](usdview-checklist.md) | usdview（prebuiltバイナリ）でのvoxelLOD variant/purpose切替の動作確認チェックリスト |
| [blender-omniverse-checklist.md](blender-omniverse-checklist.md) | Blender/Omniverseでの読み込み確認・usdviewとの表示差異記録チェックリスト |
| [payload-lazy-load-findings.md](payload-lazy-load-findings.md) | 大規模モデルに対するUSD payload遅延ロードの効果検証（計測結果と推奨） |
| [usd-wasm-webgpu-findings.md](usd-wasm-webgpu-findings.md) | usd-wasm / WebGPU Hydra delegateエコシステムの年次再評価（E6-3、初回2026-07） |

## 結論の要約

1. **独自レンダラ（Hydra render delegate）は書かない。** Hydra の設計思想
   —「シーンデータとレンダラの分離」「合成可能なシーン変換（filtering scene index）」—
   を*データ設計*に取り入れ、レンダリング自体は既存のビューワーに委ねる。
2. **経路A（USDネイティブ、先行）**: ボクセルを `UsdGeomPointInstancer` として USD レイヤーに
   オーサリングし、LOD を variantSet、メッシュ/ボクセルの切替を purpose で表現する。
   usdview / Omniverse / Blender など Hydra 系ビューワーがそのまま使える。
   `usd-core`（pip）はオーサリング可能なことを検証済み。
3. **経路B（Webビューワー、本命）**: `uv run ifc2usd serve` で起動する three.js ベースの
   自己完結 Web ビューワー。glTF（メッシュ）+ ボクセル JSON v2（InstancedMesh）を読み、
   階層ツリー・要素選択・customData 表示・ボクセル LOD 切替を提供する。
4. ボリューム場（温熱・CO₂ 等）は将来 `UsdVol` + OpenVDB で拡張する（スキーマの存在は検証済み）。
