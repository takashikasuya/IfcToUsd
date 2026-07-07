# 仕様: USD / ボクセル統合ビューワー

対象バージョン: ifc2usd 0.2 系（本仕様の実装により 0.1 → 0.2）

## 1. CLI 仕様

既存の変換をサブコマンド化する。引数なしの `ifc2usd <ifc>` は後方互換として
`convert` に読み替える。

```
ifc2usd convert <ifc> [-o out.usda] [--y-up] [-v]        # 既存機能
ifc2usd voxelize <usda|ifc> [--size 0.5]... [-o <base>]  # 占有ボクセル生成
ifc2usd export-gltf <usda> [-o out.glb]                  # 表示用 GLB 生成
ifc2usd serve <usda> [--port 8000] [--no-open]           # Web ビューワー起動
```

### 1.1 `voxelize`

- 入力: `ifc2usd convert` の出力 USD（推奨）。IFC 直接入力時は内部で convert 相当を実行。
- `--size` は複数指定可（例 `--size 1.0 --size 0.5 --size 0.25`）。各サイズが
  LOD variant になる。既定は `0.5`。
- 出力（`-o` はベース名。既定 `output/<name>_voxels`）:
  - `<base>.usda` — PointInstancer レイヤー（§3）
  - `<base>.json` — ボクセル JSON v2（§2）
- ボクセル化はメッシュの**表面占有**（surface voxelization）を既定とし、
  `--fill` で内部充填（solid）を選択可能。
- 変換元と同じ座標系・単位（m, Z-UP）で出力する。**USD 側のワールド座標での
  ボクセル AABB と、JSON の `origin + index*size` が一致すること**（受け入れ条件）。

### 1.2 `serve`

- `scene.json`（§4.1）を生成し、GLB / voxels.json / 静的アセットをローカル HTTP で配信。
- ネットワーク非依存（CDN 参照なし）。three.js は vendoring。
- `--port` 既定 8000。起動時に URL を表示（`--no-open` でブラウザ自動起動を抑止）。

## 2. ボクセル JSON スキーマ（現行 v3）

ノートブック形式（v1、暗黙）の後継が v2。v1 からの変更点: `version`/`upAxis`/`units` の明示、
`offset`→`origin`（メートル単位の実座標）、`color` の Morton 符号化廃止、LOD 複数格納。
v3（Issue #38 / E7-4）は v2 から `indices` の表現のみを変更し、他は同一。

```jsonc
{
  "version": 3,
  "units": "m",
  "upAxis": "Z",
  "source": { "usd": "ToyodaLab_structured.usda", "generator": "ifc2usd 0.2.0" },
  "origin": [-0.45, -0.5, 0.3],      // ボクセル格子原点（ワールド座標, m）
  "lods": [
    {
      "size": 0.5,                   // ボクセル一辺 (m)
      "elements": [
        {
          "guid": "20FpTZCqJy2vhVJYtjuIce",   // IFC GlobalId = USD customData GUID と一致
          "class": "IfcWall",
          "name": "壁-001",
          "color": [0.8, 0.2, 0.2],           // 0-1 正規化 RGB（Morton 化しない）
          "indices": {                        // delta + run-length 符号化（Issue #38 / E7-4）
            "base": 1234,                     // ソート済みMortonコード列の先頭値
            "deltas": [[3, 1], [7, 12], ...]  // [差分, 連続回数] の配列
          }
        }
      ]
    }
  ]
}
```

- ボクセル座標は Morton/Z-order で符号化する（3軸 21bit まで = 一辺 2,097,152 ボクセル）。
  `ix = floor((x-origin.x)/size)` 等。
- `indices` はソート済みMortonコード列を `ifc2usd.voxel.encode_morton_indices()` で
  delta + run-length 符号化したもの（`decode_morton_indices()` で復元）。ソート済み
  格納であるため隣接差分（delta）が同じ値の繰り返しになりやすく、素朴な整数リストより
  JSON出力サイズを削減できる（実測: 中実な立方体で約99.8%、表面シェルのみでも約25%以上
  の削減。`tests/test_voxel_json.py::test_indices_encoding_significantly_reduces_json_size_for_large_element`
  参照）。ビューワー（`viewer.js`の`decodeMortonIndices`）はこの符号化形式に加え、
  素朴な配列（v2互換ファイル・v1変換時の出力）も透過的に読める。
- 属性の詳細（プロパティセット）は JSON に**重複格納しない**。GUID で USD / scene.json
  側を参照する（正本の一元化）。
- 後方互換: v1（`voxelSize`/`offset`/Morton color）はビューワーで読み込み時に現行
  スキーマへ変換するローダーを用意する（`GLTF_to_Voxel.ipynb` の既存出力を捨てない）。

## 3. USD オーサリング規約（ボクセルレイヤー）

```
#usda 1.0
def Xform "IFC_Model" (
    prepend references = @ToyodaLab_structured.usda@</IFC_Model>
)
{
    def PointInstancer "Voxels" (
        variants = { string voxelLOD = "size_0_5" }
        prepend variantSets = "voxelLOD"
    )
    {
        # purpose = proxy: メッシュ(render)と切替可能
        uniform token purpose = "proxy"
        variantSet "voxelLOD" = {
            "size_1_0" { ... }
            "size_0_5" {
                point3f[] positions = [...]        # ボクセル中心（ワールド座標）
                int[] protoIndices = [...]          # 要素ごとの prototype
                rel prototypes = </IFC_Model/Voxels/Prototypes/Cube_0_5>
            }
        }
        def Scope "Prototypes" {
            def Cube "Cube_0_5" { double size = 0.5 ... }
        }
    }
}
```

規約:

- ボクセルレイヤーは**正本 usda を書き換えない**独立ファイルとし、reference で合成する。
- 要素（GUID）ごとに 1 prototype（displayColor 付き Cube）を割り当て、`protoIndices` で
  対応付ける。要素→インスタンス範囲の対応は `customData` に GUID→[start,count] の
  辞書として記録し、ビューワーからの逆引きを可能にする。
- `purpose`: ボクセル = `proxy`、メッシュ = `render`（正本側は既定のまま）。
  usdview の Display Purpose 切替でメッシュ⇔ボクセルが切り替わることが受け入れ条件。
- variant 名は `size_<m>`（`.`→`_`）。既定 variant は生成時の `--size` 先頭。

## 4. Web ビューワー仕様

### 4.1 シーン記述 `scene.json`

`serve` 起動時に USD から抽出して生成する（ビューワーは USD を直接読まない）。

```jsonc
{
  "version": 1,
  "upAxis": "Z",
  "assets": { "gltf": "model.glb", "voxels": "voxels.json" },
  "tree": [
    { "path": "/IFC_Model/Site", "name": "サイト", "class": "IfcSite",
      "guid": "...", "customData": { ... }, "children": [ ... ] }
  ]
}
```

- glTF ノード名 / voxels.json の `guid` と `tree` の `guid` が結合キー。
  glTF エクスポート時にノード `extras.guid` を必ず書き込む。

### 4.2 機能要件（MVP）

| ID | 要件 | 備考 |
| --- | --- | --- |
| FR-1 | GLB のロードと表示（PBR、displayColor フォールバック） | Z-UP をルート回転で吸収 |
| FR-2 | 階層ツリー表示（Site→…→Element）と表示/非表示トグル | scene.json の tree |
| FR-3 | クリック選択: ハイライト + プロパティパネル（class/GUID/customData） | raycast |
| FR-4 | ツリー⇔3D の双方向選択同期 | |
| FR-5 | ボクセル表示: voxels.json v2 を InstancedMesh で描画 | 要素色を反映 |
| FR-6 | メッシュ / ボクセル / 両方 の表示モード切替 | 経路Aの purpose に相当 |
| FR-7 | ボクセル LOD 切替（lods 配列から選択） | |
| FR-8 | ボクセル選択時も GUID 逆引きで同じプロパティパネルを表示 | Morton→要素の逆引き |
| FR-9 | カメラ: orbit / pan / zoom / 全体フィット / 選択フィット | OrbitControls |
| FR-10 | 断面（クリップ平面）1 枚（Z 高さスライダー） | 階別の確認用 |

### 4.3 非機能要件

| ID | 要件 |
| --- | --- |
| NFR-1 | ToyodaLab（Mesh 76 / Material 21）で初期表示 3 秒以内・操作 30fps 以上（普及帯ノート GPU） |
| NFR-2 | ボクセル 50 万インスタンスまで操作可能（InstancedMesh 1 draw call / LOD） |
| NFR-3 | 完全オフライン動作（CDN・外部フォント参照なし）。`uv sync` 以外のビルド工程なし |
| NFR-4 | ブラウザ: 現行 Chrome / Edge / Safari（WebGL2）。WebGPU は将来オプション |
| NFR-5 | `uv run pytest` に voxelize / scene.json の回帰テストを追加（フィクスチャで検証） |

### 4.4 検証方法

1. `uv run ifc2usd convert tests/fixtures/minimal.ifc` → `voxelize --size 0.5 --size 0.25`
   → JSON/USD の整合（AABB 一致、GUID 対応、Morton 逆変換）を pytest で検証。
2. `serve` を起動し Playwright（本環境に導入済み）で FR-1〜FR-8 をスクリーンショット検証。
3. 経路A: 生成した `<base>.usda` を usdview（prebuilt）で開き、voxelLOD variant と
   purpose 切替を目視確認（手動・リリース前チェックリスト）。
