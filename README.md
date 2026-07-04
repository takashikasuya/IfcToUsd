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

### オプション

| 引数 | 説明 |
| --- | --- |
| `ifc_path` | 入力する `.ifc` ファイル（必須） |
| `-o, --output` | 出力する `.usd` / `.usda` パス（既定: `output/<name>_structured.usda`） |
| `--y-up` | Y-UP 軸で出力（既定は IFC 標準の Z-UP） |
| `-v, --verbose` | 詳細ログを出力 |

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

## リポジトリ構成

- `ifc2usd/` — IFC→USD 変換の Python パッケージ / CLI（本体）
  - `cli.py` — CLI エントリポイント
  - `ifc.py` — IFC ジオメトリ・プロパティの抽出（ifcopenshell 0.8 対応）
  - `usd.py` — USD ステージの構築（メッシュ・マテリアル・階層）
- `IFC_to_USD.ipynb` — 変換ロジックのもとになった Jupyter ノートブック（互換のため保持）
- `IFC_to_GLTF.ipynb` / `IFC_to_RDF.ipynb` / `GLTF_to_Voxel.ipynb` — 関連する変換ノートブック（本パッケージの対象外）
- `files/` — サンプル IFC モデル
