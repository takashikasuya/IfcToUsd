# usdview 動作確認チェックリスト（E4-1）

`ifc2usd voxelize` が生成する PointInstancer ボクセルレイヤー（`<base>.usda`、
`docs/viewer/spec.md` §3）を、経路A（Hydra系ビューワー）の代表として usdview
（NVIDIA prebuilt バイナリ）で目視確認するための手順。

> **本チェックリストの実行について**: usdview はデスクトップGUIアプリケーションで
> あり、このサンドボックス環境（ヘッドレス、prebuilt usdview 未導入）では実行できない。
> そのため以下の手順・確認項目はドキュメント化のみで、実際の実行とスクリーンショットの
> 取得はusdviewが利用可能な環境（開発者のローカルマシン等）で行う必要がある。
> 各手順に `📷 スクリーンショット:` の記載箇所を用意してあるので、実施時にそこへ
> 画像を追加すること。

## 前提

- usdview（prebuilt バイナリ）をインストール済みであること。
  [NVIDIA の公式手順](https://docs.nvidia.com/learn-openusd/latest/usdview-install-instructions.html)
  を参照（`usd-core`（pip）には usdview は同梱されない — Qt ベースのGUIは別配布物）。
- 本リポジトリで `uv sync` 済みであること（`ifc2usd` コマンドの実行に必要）。

## 1. 検証用データの生成

```bash
uv sync
uv run ifc2usd convert files/ToyodaLab.ifc -o output/ToyodaLab_structured.usda
uv run ifc2usd voxelize output/ToyodaLab_structured.usda --size 1.0 --size 0.5
```

`output/` に以下が生成されることを確認する:

- [ ] `ToyodaLab_structured.usda`（正本USD）
- [ ] `ToyodaLab_structured_voxels.json`（ボクセルJSON v2）
- [ ] `ToyodaLab_structured_voxels.usda`（PointInstancerボクセルレイヤー、本チェックリストの対象）

## 2. usdview で開く

```bash
usdview output/ToyodaLab_structured_voxels.usda
```

- [ ] エラーダイアログなく起動し、3Dビューポートに建物モデルが表示される
- [ ] 左側の Prim Browser（階層ツリー）で `/IFC_Model` 以下に `Site` → `Building` →
      `Storey` → 各要素、および `/IFC_Model/Voxels`（PointInstancer）が見えること
- [ ] `/IFC_Model/Voxels` を選択すると、Properties パネルの Meta Data セクションに
      `variantSets = ["voxelLOD"]` と `customData` の `elementRanges`
      （GUID→[start, count] の辞書）が確認できること

📷 スクリーンショット: 起動直後の全体像（Prim Browserで`/IFC_Model/Voxels`を選択した状態）

## 3. voxelLOD variant 切替の確認

1. Prim Browser で `/IFC_Model/Voxels` を選択する
2. Properties パネル（またはメニュー `Select` → `Variants`、usdviewのバージョンに
   よりメニュー位置が異なる場合は `Edit Variants` 相当の項目を探す）で `voxelLOD`
   variant set のドロップダウンを開く
3. 生成時に指定した `--size` の数だけ variant（`size_1_0`, `size_0_5`）が
   選択肢として並んでいることを確認する

- [ ] `size_1_0` を選択 → 粗いボクセル（1m格子）が表示される
- [ ] `size_0_5` を選択 → より細かいボクセル（0.5m格子、`size_1_0`よりボクセル数が多い）
      に切り替わる
- [ ] 既定選択（起動直後）は `--size` に最初に指定したサイズ（この例では `size_1_0`）
      になっていること

📷 スクリーンショット: `size_1_0` 選択時 / `size_0_5` 選択時（並べて比較できるとよい）

## 4. purpose（render/proxy）切替の確認

`/IFC_Model/Voxels` の `purpose` は `proxy`、正本のメッシュ側は既定（`render`扱い）。
usdview の View メニュー（バージョンにより `View` → `Display Purpose`、または
ツールバーの表示切替アイコン）で Proxy 表示のオン/オフを切り替える。

- [ ] Proxy 表示 OFF → ボクセル（PointInstancer）が非表示になり、建物メッシュのみ見える
- [ ] Proxy 表示 ON → ボクセルとメッシュが同時に見える（重なって表示される）
- [ ] メッシュ自体は常に表示されたまま（`render` purposeなので Proxy 切替の影響を受けない）

📷 スクリーンショット: Proxy OFF（メッシュのみ）/ Proxy ON（メッシュ+ボクセル）

## 5. 正本USDが書き換えられていないことの確認

```bash
usdview output/ToyodaLab_structured.usda
```

- [ ] 正本USD単体を開いても問題なく表示され、`/IFC_Model/Voxels` prim は存在しない
      （ボクセルレイヤーは正本を書き換えない独立ファイルであることの確認）
- [ ] 階層（Site/Building/Storey/Element）・マテリアル・GUID等のcustomDataが
      `ifc2usd convert` 直後と変わっていない

## 結果の記録

実施日・usdviewバージョン・確認結果（各チェック項目のPASS/FAIL）・スクリーンショットを
本ファイルに追記するか、実施記録として別途保存すること。
