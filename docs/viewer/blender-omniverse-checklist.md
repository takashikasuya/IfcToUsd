# Blender / Omniverse 動作確認チェックリスト（E4-2）

`ifc2usd`が生成するUSD（`convert`の正本、`voxelize`のPointInstancerボクセルレイヤー）を
Blender・NVIDIA Omniverseで読み込み、usdview（`docs/viewer/usdview-checklist.md`）との
表示差異を記録するための手順。

> **本チェックリストの実行について**: BlenderもOmniverseもデスクトップGUI
> アプリケーションであり、このサンドボックス環境（ヘッドレス）では実行できません
> （`which blender`/`which omniverse`はいずれも未検出）。そのため本チェックリストは
> ドキュメント化のみで、実際の実行・差異の記録はBlender/Omniverseが利用可能な環境
> （開発者のローカルマシン等）で行う必要があります。usdview-checklist.mdと異なり、
> 「既知の差異」として断定的に書ける項目がほぼありません — Blender/Omniverse双方とも
> USDインポート機能はバージョンごとに改善が続いており、このリポジトリのIFC変換結果を
> 実際に読み込んで確認するまでは、variant切替やpurpose切替の挙動を確信を持って
> 記載できないためです。各項目は「確認して記録する」チェックリストとして書いています。

## 前提

- 検証用データは`docs/viewer/usdview-checklist.md`の手順1（`convert` → `voxelize`）で
  生成した`output/ToyodaLab_structured.usda`と`output/ToyodaLab_structured_voxels.usda`
  を使う
- Blender: USDインポート機能はBlender 3.0以降に標準搭載（バージョンによりサポート
  範囲が異なるため、確認したバージョンを必ず記録すること）
- NVIDIA Omniverse: [Omniverse Launcher](https://www.nvidia.com/en-us/omniverse/download/)
  経由でUSD Composer（旧Create）または最小構成のViewをインストール

## 1. Blenderでの確認

`File > Import > Universal Scene Description (.usd*)` で
`output/ToyodaLab_structured.usda`を読み込む。

- [ ] エラーなくインポートできる
- [ ] Site/Building/Storey/Elementの階層がBlenderのアウトライナーに反映されている
- [ ] 各要素にメッシュが表示され、USD側のdiffuseColor/PBR値がBlenderのマテリアル
      （Principled BSDF等）に反映されている（色が概ね一致する）
- [ ] GUID/class等のcustomDataがBlender側でどう見える/見えないか記録する
      （Blenderのカスタムプロパティとしてインポートされるか、破棄されるか）

続けて`output/ToyodaLab_structured_voxels.usda`を読み込む:

- [ ] PointInstancer（ボクセル）がBlenderにインポートされ、インスタンスとして
      表示される、またはインポートできない/インスタンスが展開されないなど
      挙動を記録する
- [ ] `voxelLOD` variantSetがBlenderのUI（プロパティパネルのVariant Sets等）から
      切替可能か確認し、記録する（インポート時に単一variantへ静的に解決されて
      切替不可の場合はその旨を記録する）
- [ ] `purpose=proxy`（ボクセル）と正本メッシュ（`purpose`未設定=render相当）の
      表示切替がBlender側の機能（Viewport Overlays等）で可能か確認し、記録する

## 2. Omniverseでの確認

USD Composer（または同等のHydraベースビューワー）で同じファイルを開く。

- [ ] エラーなく開ける
- [ ] 階層・メッシュ・マテリアル色がusdviewと一致する
- [ ] `voxelLOD` variantSetの切替がusdview同様にライブで行えることを確認する
      （OmniverseはHydra/USDネイティブのため、usdviewに近い挙動が期待されるが
      実機で確認すること）
- [ ] `purpose`（render/proxy）切替がusdview同様に行えることを確認する

## 3. 差異の記録

上記の各チェック項目について、usdviewでの見え方（`usdview-checklist.md`参照）との
差異を以下の形式で本ファイルに追記する:

```
### 確認日: YYYY-MM-DD / Blenderバージョン: x.y / Omniverseアプリ: xxx (version)

- [項目名]: usdviewでは○○だったが、Blenderでは△△だった（差異あり/差異なし）
```

差異が無い場合も「差異なし」として明示的に記録すること（未実施と区別するため）。
