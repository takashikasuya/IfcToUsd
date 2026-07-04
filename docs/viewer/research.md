# 調査結果: USD / ボクセル統合ビューワー

日付: 2026-07-04
対象: `ifc2usd` の USD 出力と `GLTF_to_Voxel.ipynb` のボクセル出力を統合表示するビューワー

## 1. リポジトリ内資産の棚卸し

### 1.1 USD 出力（`ifc2usd`）

- `output/<name>_structured.usda`。Z-UP・メートル単位。
- 階層: `/IFC_Model`(assembly) → `Site` → `Building` → `Storey_*` → `Space_*` / `Element_*` → `mesh`。
- 各 prim に `customData`（GUID / class / Name / 緯度経度）、メッシュに UsdPreviewSurface
  マテリアルと displayColor。ToyodaLab 実測: Mesh 76・Material 21。
- **選択・ツリー・メタデータ表示に必要な意味情報は既に USD 側に揃っている。**

### 1.2 ボクセル出力（`GLTF_to_Voxel.ipynb`）

ノートブックは glTF(GLB) を trimesh で読み、要素ごとに `trimesh.voxelized()` で
占有ボクセル化して JSON を書き出す。実質的な現行フォーマット:

```jsonc
{
  "voxelSize": 0.5,              // LOD: 0→1000mm, 1→500mm, 2→250mm, 3→100mm
  "offset": [i, j, k],           // シーン AABB 最小点のボクセル座標（負値の原点合わせ）
  "elements": [{
    "guid": "IFC GlobalId",
    "name": "要素名",
    "class": "IfcWall など",
    "indices": [123, ...],       // pymorton.interleave3(x,y,z) — Morton/Z-order 符号化
    "color": 98304,              // interleave3(r,g,b) — RGB も Morton 符号化（※）
    "metadata": { ... }          // IFC プロパティの残り
  }]
}
```

特徴と課題:

- **要素単位のスパース占有ボクセル + セマンティクス**という設計は良い（GUID で USD と結合できる）。
- Morton 符号化はストリーミング・空間索引と相性が良い一方、`color` の Morton 化は
  ノートブック内でも「特に意味はない」とコメントされており、v2 では素の RGB に戻すべき。
- 生成が glTF 経由（別ノートブック依存）で、`ifc2usd` の変換行列・単位系と独立に実装
  されているため、**USD 側とボクセル側で座標が一致する保証が現状ない**。
  → ボクセル化を `ifc2usd voxelize` としてパッケージ側に取り込み、同じジオメトリ
  ソースから生成するのが筋。
- 過去コミット「add viewer」(d236255) はノートブック内の trimesh 表示追加のみで、
  独立したビューワー資産は存在しない。

### 1.3 実行環境の制約（検証済み）

- PyPI の **`usd-core` にはイメージング系（`UsdImaging` / `UsdImagingGL` / `Glf` /
  `UsdAppUtils`）が含まれない** — 本環境で import 不可を確認。
  つまり現行依存のままでは Python から Hydra レンダリングはできない。
- 一方 **`UsdGeom.PointInstancer` / `UsdVol.Volume` / `UsdVol.OpenVDBAsset` の
  オーサリングは可能**（import 確認済み）。「表示可能な USD を書く」ことは現行依存で完結する。
- usdview を使うには [NVIDIA の prebuilt OpenUSD バイナリ等](https://docs.nvidia.com/learn-openusd/latest/usdview-install-instructions.html)
  が別途必要（pip では入らない）。

## 2. Hydra アーキテクチャの要点

出典: [Hydra 2.0 Getting Started Guide](https://openusd.org/dev/api/_page__hydra__getting__started__guide.html)、
[Learn OpenUSD: Hydra](https://docs.nvidia.com/learn-openusd/latest/beyond-basics/hydra.html)、
[HdStorm](https://openusd.org/dev/api/hd_storm_page_front.html)

- Hydra は**シーンデータとレンダラを分離する仲介層**。データ側は Hydra 2.0 で
  scene index（`HdSceneIndex`）に世代交代し、`UsdImagingStageSceneIndex` を起点に
  **filtering scene index を連鎖**させてシーンを段階変換する（25.11 で Hydra 2 が既定化）。
- レンダラ側は render delegate（`HdRenderDelegate`）として差し替え可能。標準の
  **Storm** は Hgi 抽象化により OpenGL / Metal / Vulkan で動く実時間レンダラで、
  usdview が使用。他に Embree、RenderMan、Omniverse RTX などが同じ枠組みに載る。
- 本プロジェクトへの示唆:
  1. **「1つの正本シーン + 合成可能な派生変換 + 差し替え可能な表示先」**という構図を
     そのまま借りる。IFC→USD（正本）→ ボクセル化・glTF 化（filtering scene index に相当）
     → usdview / Web / Omniverse（render delegate に相当）。
  2. C++ で独自 render delegate を実装する規模ではない。**USD を「Hydra 系ビューワーが
     正しく表示できる形」でオーサリングすることが、最小コストで Hydra エコシステム全体
     （usdview / Omniverse / Blender / Houdini）を味方につける方法**。
  3. LOD・表現切替は Hydra/USD の既存機構（variantSet、purpose: render/proxy/guide、
     payload）で表現すれば、どのビューワーでも UI から操作できる。

## 3. ビューワー選択肢の比較

| 選択肢 | 種別 | ボクセル | 導入コスト | 評価 |
| --- | --- | --- | --- | --- |
| usdview（prebuilt バイナリ） | デスクトップ / Hydra Storm | PointInstancer で可 | 中（別途配布物） | 開発者向け検証に最適。エンドユーザー配布には不向き |
| NVIDIA Omniverse | デスクトップ+RTX / Hydra | PointInstancer / UsdVol | 中〜高 | 高品質。NVIDIA GPU 前提 |
| Blender (USD import) | デスクトップ | PointInstancer は展開される | 低 | 手軽な目視確認。挙動はバージョン依存 |
| three.js + glTF 自作ビューワー | Web (WebGL/WebGPU) | InstancedMesh で自前実装 | 中（自作） | **配布性最良・完全制御。ボクセル JSON をそのまま活かせる** |
| [needle-tools/usd-viewer](https://github.com/needle-tools/usd-viewer)（Autodesk WASM 系譜） | Web / usd-wasm + three.js delegate | 未知数 | 中 | USDZ/USD を直接読める。WASM ビルドの保守リスクは追う必要あり |
| [Cinevva usdjs](https://app.cinevva.com/guides/threejs-usdc-tech-report.html) | Web / pure JS | 未知数 | 中 | USDC を JS で解析。若いエコシステム |
| Autodesk WebGPU Hydra delegate ([経緯](https://forums.autodesk.com/t5/engineering-hub-blog/autodesk-open-sources-web-based-usd-viewing-implementation/ba-p/11071751)) | Web / WebGPU | — | 高 | three.js 非互換方向へ転換中。ウォッチ対象 |

Web での USD 直接表示は [AOUSD フォーラム](https://forum.aousd.org/t/what-is-the-current-state-of-usd-and-best-practices-for-web-viewing/2528)
でも「発展途上で決定版なし」が現状認識。**Web 経路は glTF + ボクセル JSON を入力にするのが
2026 年時点で最も堅実**（glTF は本リポジトリに既に `IFC_to_GLTF.ipynb` の系譜がある）。

## 4. ボクセル・ボリュームの USD 表現

| データ | USD 表現 | 表示側 | 備考 |
| --- | --- | --- | --- |
| 占有ボクセル（要素単位・スパース） | `UsdGeomPointInstancer`（立方体プロトタイプ + positions） | Hydra 全般が高速描画 | 数万〜数十万インスタンスまで実用的。GUID は instance ごとでなく要素 prim 単位で保持 |
| ボクセル LOD (1000/500/250/100mm) | variantSet `voxelLOD` | ビューワー UI から切替 | payload 化で遅延ロードも可能 |
| メッシュ/ボクセル切替 | purpose（mesh=render, voxel=proxy） | usdview 標準機能で切替 | Web 側もフラグで対応 |
| 連続場（温熱・CO₂・煙） | [`UsdVol.Volume` + `OpenVDBAsset`](https://openusd.org/dev/user_guides/schemas/usdVol/overview.html) | Storm/RTX が対応、Web はレイマーチ自作 | [OpenVDB](https://www.openvdb.org/about/) はスパースボリュームの事実上標準。将来フェーズ |

参考資料（ユーザー提供の空間解析カーネル調査）との整合: 同調査の
「exact layer(IFC/B-Rep) → derived layer(mesh/voxel/SDF/tile) → semantic layer →
visualization layer」の層分けに対し、本ビューワー計画は **derived layer と
visualization layer の接続部分を最小実装するもの**と位置づけられる。将来の 3D Tiles
配信・SDF 解析・時系列結合は backlog の後期エピックとして接続可能。

## 5. 主要な出典

- [OpenUSD: Hydra 2.0 Getting Started Guide](https://openusd.org/dev/api/_page__hydra__getting__started__guide.html)
- [NVIDIA Learn OpenUSD: Hydra](https://docs.nvidia.com/learn-openusd/latest/beyond-basics/hydra.html)
- [OpenUSD: HdStorm](https://openusd.org/dev/api/hd_storm_page_front.html)
- [OpenUSD: UsdVol overview](https://openusd.org/dev/user_guides/schemas/usdVol/overview.html)
- [usdview インストール（prebuilt バイナリ）](https://docs.nvidia.com/learn-openusd/latest/usdview-install-instructions.html)
- [needle-tools/usd-viewer](https://github.com/needle-tools/usd-viewer) /
  [Autodesk Web USD 公開の経緯](https://forums.autodesk.com/t5/engineering-hub-blog/autodesk-open-sources-web-based-usd-viewing-implementation/ba-p/11071751)
- [AOUSD: Web での USD 表示の現状](https://forum.aousd.org/t/what-is-the-current-state-of-usd-and-best-practices-for-web-viewing/2528) /
  [USDZ on web](https://forum.aousd.org/t/state-of-the-art-for-viewing-usdz-on-web/2344)
- [ASWF USD Working Group: USD Web Visualization](https://lf-aswf.atlassian.net/wiki/display/WGUSD/USD+Web+Visualization)
- [OpenVDB](https://www.openvdb.org/about/)
