# IFC2USD

IFC（建築BIMデータ）を構造化された [OpenUSD](https://openusd.org/) ステージへ変換するプログラム。

空間階層（Site → Building → Storey → Space / Element → Object）を USD の Xform 階層として再現し、
各エレメントにメッシュ・UsdPreviewSurface マテリアル・IFC由来のメタデータ（GUID・クラス・名称・緯度経度など）を付与する。

## セットアップと実行（uv）

以前は conda で環境を作り USD をソースからビルドする必要があったが、現在は
[`usd-core`](https://pypi.org/project/usd-core/)（OpenUSD）と
[`ifcopenshell`](https://pypi.org/project/ifcopenshell/) が PyPI で配布されているため、
[uv](https://docs.astral.sh/uv/) だけで完結する。

```bash
# 依存関係のインストール（.venv を自動作成）
uv sync

# 変換の実行（既定で output/<name>_structured.usda を出力）
uv run ifc2usd files/ToyodaLab.ifc

# 出力先や座標系を指定
uv run ifc2usd files/ToyodaLab.ifc -o output/model.usda --y-up --verbose

# モジュールとしても起動可能
uv run python -m ifc2usd files/ToyodaLab.ifc
```

### オプション（`convert`）

| 引数 | 説明 |
| --- | --- |
| `ifc_path` | 入力する `.ifc` ファイル（必須） |
| `-o, --output` | 出力する `.usd` / `.usda` パス（既定: `output/<name>_structured.usda`） |
| `--y-up` | Y-UP 軸で出力（既定は IFC 標準の Z-UP） |
| `-v, --verbose` | 詳細ログを出力 |

## ボクセル化（`voxelize`）

変換済みの USD（推奨）または IFC を直接、メッシュの表面占有ボクセルへ変換する。
複数の `--size` を指定すると LOD（詳細度）ごとのボクセルデータになる。

```bash
# 変換済みUSDから（推奨）
uv run ifc2usd voxelize output/ToyodaLab_structured.usda --size 1.0 --size 0.5

# IFCから直接（内部でconvert相当を実行）
uv run ifc2usd voxelize files/ToyodaLab.ifc --size 0.5

# 内部充填（既定は表面のみ）
uv run ifc2usd voxelize output/ToyodaLab_structured.usda --fill
```

出力（`-o` はベース名。既定 `output/<name>_voxels`）:

- `<base>.json` — ボクセル JSON v2（`docs/viewer/spec.md` §2）。要素(GUID)ごとに
  Morton(Z-order)符号化された占有ボクセルの座標と色を格納する
- `<base>.usda` — PointInstancer ボクセルレイヤー。正本USDを書き換えない独立ファイルで、
  `voxelLOD` variantSet で `--size` ごとのLODを切替できる（`usdview` での確認は
  `docs/viewer/` を参照）

## glTF エクスポート（`export-gltf`）

変換済み USD を glTF(GLB) へ書き出す。Web ビューワー（後述）が内部で使う形式でもある。

```bash
uv run ifc2usd export-gltf output/ToyodaLab_structured.usda -o output/ToyodaLab.glb
```

## Web ビューワー（`serve`）

変換済み USD を、ブラウザで動く three.js ベースのローカル Web ビューワーとして配信する。
CDN 参照なし（three.js は同梱）で、ネットワーク遮断環境でも動作する。

```bash
uv run ifc2usd serve output/ToyodaLab_structured.usda
# --port 8000 (既定) / --no-open でブラウザ自動起動を抑止
```

機能: 階層ツリー表示・表示/非表示切替、3Dクリックまたはツリーからの選択（GUID/class/
customDataを表示するプロパティパネル付き）、メッシュ/ボクセル/両方の表示モード切替と
ボクセルLOD切替（ボクセル化可能な要素があれば自動生成される）。

## テスト

小さな合成フィクスチャ（Site/Building/Storey に色付きの壁2枚）を変換し、座標系・階層・
ジオメトリ・マテリアル・ボクセル化・glTFエクスポートを検証するテストを同梱している。
外部データ（`files/ToyodaLab.ifc`）不要で動く。Web ビューワー関連は Playwright
（`uv sync` の dev グループで導入済み）による実ブラウザE2Eテスト。

```bash
uv run pytest                              # 全テストの実行
uv run python tests/generate_fixture.py    # tests/fixtures/minimal.ifc の再生成
```

## 出力の確認

生成された USD は以下で開ける:

- `usdview`（[OpenUSD ツール](https://openusd.org/release/toolset.html)）
- Blender（USD インポート）
- NVIDIA Omniverse

シーン単位はメートル（`metersPerUnit = 1.0`）。

## Requirements

`pyproject.toml` に定義（`uv sync` で解決）:

- Python >= 3.10
- [usd-core](https://pypi.org/project/usd-core/)（OpenUSD）
- [ifcopenshell](https://pypi.org/project/ifcopenshell/) >= 0.8
- numpy
- [tqdm](https://tqdm.github.io/)
- [trimesh](https://trimesh.org/)（glTF エクスポート）
- [rtree](https://pypi.org/project/rtree/)（`voxelize --fill` の内部充填判定に使用）

## リポジトリ構成

- `ifc2usd/` — IFC→USD 変換・ボクセル化・glTF/Webビューワー配信の Python パッケージ / CLI（本体）
  - `cli.py` — CLI エントリポイント（`convert`/`voxelize`/`export-gltf`/`serve`）
  - `ifc.py` — IFC ジオメトリ・プロパティの抽出（ifcopenshell 0.8 対応）
  - `usd.py` — USD ステージの構築（メッシュ・マテリアル・階層）、`elements_from_stage`
    による変換済みUSDからのボクセル化対象要素の抽出
  - `voxel.py` — メッシュの表面/内部占有ボクセル化、Morton(Z-order)符号化、
    ボクセル JSON v2 ライター、PointInstancer ボクセルレイヤーライター
  - `gltf.py` — USD→glTF(GLB) エクスポート
  - `scene_index.py` — USD→`scene.json`（Web ビューワー用の階層・customData抽出）
  - `serve.py` — Web ビューワー用の静的ファイル一式（GLB/scene.json/voxels.json）の
    組み立てとローカル HTTP サーバー
  - `viewer/` — three.js ベースの Web ビューワー（ビルド不要、静的ファイル）。
    GLB表示・階層ツリー・クリック選択・ボクセル描画・表示モード切替を実装
- `IFC_to_USD.ipynb` — 変換ロジックのもとになった Jupyter ノートブック（互換のため保持）
- `IFC_to_GLTF.ipynb` / `IFC_to_RDF.ipynb` / `GLTF_to_Voxel.ipynb` — 関連する変換ノートブック（本パッケージの対象外）
- `files/` — サンプル IFC モデル
- `docs/viewer/` — USD / ボクセル統合ビューワーの調査・アーキテクチャ・仕様・バックログ
