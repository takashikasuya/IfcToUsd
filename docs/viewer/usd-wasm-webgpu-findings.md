# usd-wasm / WebGPU Hydra delegate 年次再評価（E6-3）

`docs/viewer/README.md`の結論1（「独自レンダラ（Hydra render delegate）は書かない」）を、
ブラウザでOpenUSD/Hydraをネイティブに扱うエコシステムの進展を踏まえて年次で見直すための
調査記録。今回が初回評価（2026-07）。

## 背景・本Issueが確認したいこと

本リポジトリのWebビューワー（`ifc2usd/viewer/viewer.js`）は、USD/Hydraを一切ブラウザで
扱わない。事前に`export-gltf`で焼き込んだglTF + 独自JSON（`scene.json`/`voxels.json`）を
three.jsで読むだけの自前実装（経路B、`docs/viewer/README.md`参照）。この設計判断（経路A
＝USDネイティブ、経路B＝Web の2系統分離）が、ブラウザでUSD/Hydraをネイティブ表示する
選択肢の成熟度によって今も妥当かを、年1回問い直すのがE6-3の役割。

## 調査結果（2026-07時点）

### 1. "usd-wasm"という名称の前提修正

調査時点でNVIDIAの`usd-wasm`という名前のプロジェクトは存在しない
（`github.com/NVIDIA-Omniverse/usd-wasm`は404）。実際の系譜は **Autodesk → Needle Tools**:

- Autodesk がWASM USDバインディング + three.js向けHydraレンダーデリゲートをOSSとして公開
  （その後社内ではWebGPU向けデリゲートへ軸足を移したが、three.js互換ではない）
  [Autodesk forums](https://forums.autodesk.com/t5/engineering-hub-blog/autodesk-open-sources-web-based-usd-viewing-implementation/ba-p/11071751)
- **Needle Tools** がAutodeskのthree.js版デリゲートを引き継ぎ実運用レベルまで仕上げたのが
  **[`needle-tools/usd-viewer`](https://github.com/needle-tools/usd-viewer)**
  （OpenUSDをWebAssemblyへコンパイルし、three.js Hydraブリッジで描画）。
  約396コミット・18リリース、**[実際に動くライブデモ](https://usd-viewer.needle.tools/)**あり。
  ライセンスは **PolyForm Noncommercial 1.0.0**（商用利用はNeedleへの個別連絡が必要 —
  採用を検討する場合の実務上の制約）。正確な最終コミット日時までは確認できていない
  （本セッションのGitHub APIアクセスは本リポジトリに限定されており、github.com自体への
  直接フェッチは403で拒否されるため）。
- **上流OpenUSD v26.03**（Alliance for OpenUSD、2026年3月）で正式に **WASMビルド対応**
  （wasm32/wasm64、Emscripten経由）と`wasmFetchResolver`サンプルが追加された
  （[aousd.org](https://aousd.org/blog/openusd-v26-03/)）。ただし重要な注意点: これは
  「ブラウザでのシーン合成・データアクセス」であって、GPUレンダラーが同梱されるわけではない
  —— 描画には依然としてWebGL/WebGPUへのブリッジ（＝Needleが提供しているもの）が必要。

### 2. WebGPU Hydraレンダーデリゲート（一般）

依然として、汎用的に使える実運用レベルのネイティブWebGPU Hydraデリゲートは存在しない。
AutodeskがPBRシェーディングを行うWebGPU Hydraデリゲートを試作したという情報はあるが、
採用可能な製品としては公開されておらずthree.js非互換。Pixar自身のリアルタイムデリゲート
（HdStorm）はOpenGL/Vulkan/Metalが対象で**WebGPUは対象外**
（[openusd.org](https://openusd.org/dev/api/hd_storm_page_front.html)）。つまり現時点で
成熟しているブラウザ経路は「three.js版Hydraデリゲート」（Needle）であり、
「ネイティブWebGPU Hydraデリゲート」ではない。ネイティブWebGPU側は依然として研究・PoC段階。

### 3. 隣接領域: three.jsのWebGPU対応・ブラウザ側WebGPU普及率

- three.jsの`WebGPURenderer`は**r171（2025年9月）以降、実用レベル**——
  `WebGLRenderer`からの置き換えはほぼ1行で済み、WebGL2への自動フォールバックも持つ
  （[three.js docs](https://threejs.org/docs/pages/WebGPURenderer.html)）。
- WebGPUは**2026年1月に主要ブラウザ全体で「Baseline」到達**: Safari 26
  （iOS/macOS Tahoe、2025年9月）、Firefox 141（Windows）/145（macOS ARM、Linuxは2026年内予定）、
  Chrome/Edgeは既存対応。世界カバー率は**約82〜85%**（caniuse調べ、2026年3月時点で約84.68%）、
  残りはWebGL2へ自動フォールバック（[caniuse](https://caniuse.com/webgpu)）。

## 結論・推奨

**状況は変化したが、「独自Hydraデリゲートを書く」方向への変化ではない。** 2024〜2025年から
本当に変わった点は次の2つ:

1. WASM経由でのブラウザ内リアルUSD表示が実運用レベルに達した（Needle）
2. 上流OpenUSD自体がWASMビルドを公式サポートし始めた（v26.03）

**ただし、成熟している選択肢（Needleの`usd-viewer`）自体がthree.js版Hydraデリゲートであり、
本リポジトリのthree.js採用判断を裏付けこそすれ、置き換える理由にはならない。** E6-3が本来
問うている「ネイティブWebGPU Hydraデリゲート」は依然として未成熟で、小規模チームが
採用できる段階にない。

**推奨: 引き続き「独自Hydraデリゲートは書かない」。** ただし今年の実質的な変化として、
「three.jsを使う」ことと「（glTF焼き込みではなく）実際のUSD/Hydraをレンダリングする」ことが
**もはや二者択一ではなくなった**点は記録に値する。Needleのstack（WASM-USD + three.js Hydra
ブリッジ）を使えば、現行の`export-gltf`焼き込みステップを置き換えつつthree.jsは維持できる
可能性がある。ただちに置き換えを提案するのではなく、**時間を区切ったPoCスパイク**を将来の
候補として記録する（検討すべき障壁: PolyForm Noncommercialライセンスでの商用可否、WASM
ペイロードサイズ、本リポジトリのIFC customData/階層ツリーUXを保てるか）。

**次回評価予定日: 2027-07**（ネイティブWebGPU Hydraデリゲートの実用化状況を中心に再確認する）。

## 出典

- https://github.com/needle-tools/usd-viewer
- https://usd-viewer.needle.tools/
- https://forums.autodesk.com/t5/engineering-hub-blog/autodesk-open-sources-web-based-usd-viewing-implementation/ba-p/11071751
- https://aousd.org/blog/openusd-v26-03/
- https://digitalproduction.com/2026/03/26/openusd-v26-03-gets-the-splats/
- https://github.com/PixarAnimationStudios/OpenUSD/issues/1492
- https://lf-aswf.atlassian.net/wiki/display/WGUSD/USD+Web+Visualization
- https://www.khronos.org/developers/linkto/usd-and-materialx-on-the-web
- https://openusd.org/dev/api/hd_storm_page_front.html
- https://threejs.org/docs/pages/WebGPURenderer.html
- https://www.utsubo.com/blog/threejs-2026-what-changed
- https://vr.org/articles/webgpu-baseline-2026-three-js-webxr-default
- https://caniuse.com/webgpu
- https://web.dev/blog/webgpu-supported-major-browsers
- https://github.com/gpuweb/gpuweb/wiki/Implementation-Status

未確認のまま断定を避けた点（今後の評価で埋める余地）: Needle `usd-viewer`の正確な
最終コミット日時、AutodeskのWebGPUデリゲートの正確な現状。
