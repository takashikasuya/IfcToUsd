# USD payload による遅延ロード検証（E4-3）

大型IFCモデルに対し、USDの`payload`合成弧を使うことで初期表示（`Usd.Stage.Open()`）を
短縮できるかを検証した記録。

## 背景・対象範囲

この効果が意味を持つのは**経路A（USDネイティブ、usdview/Omniverse/Blender）**のみ。
本リポジトリの「本命」である**経路B（Webビューワー）**は、USD合成弧を一切読まず
事前に焼き込んだglTF + `scene.json` + `voxels.json`を読むため（`docs/viewer/README.md`
参照）、`payload`によるUSD側の遅延ロードとは無関係。この検証はusdview等での
大規模モデル閲覧を将来サポートする際の判断材料として位置づける。

## 手法

`files/ToyodaLab.ifc`（76要素、変換後USD 9.7MB）を素材に、15棟分コピーした
「大規模モデル」を模擬した（実際に入手可能な大規模IFCサンプルが手元に無いため。
実データでの再検証が望ましい旨は末尾に記載）:

```
wings/wing_1.usda 〜 wing_15.usda   (各9.7MB、計146.5MB、要素合計1140)
```

これを2通りのルートレイヤーから束ねた:

- `reference_root.usda`: 15個のXform prim、それぞれが`references`で各wingを
  常時合成（USDの標準的な組み方。今の`ifc2usd`はこの形に相当）
- `payload_root.usda`: 同じ構造だが`references`の代わりに`payload`を使用
  （`Usd.Stage.Open(..., load=Usd.Stage.LoadNone)`でロードを後回しにできる）

計測は**プロセスごとに独立したPython実行**で行った（同一プロセス内で同じパスを
繰り返し`Stage.Open`すると、USDの内部レイヤーキャッシュにより2回目以降が
異常に高速化され、実際の「初回オープン」を代表しない値になることを確認したため。
各設定3回のコールドプロセス実行の最小値を採用）。

## 結果

| 構成 | 時間（最小、3回中） | ベースライン比 |
| --- | --- | --- |
| `reference_root.usda`（ベースライン、全データ常時合成） | 0.421s | 1.0x |
| `payload_root.usda`, `load=LoadAll`（payload・即時ロード） | 0.425s | ほぼ同等 |
| `payload_root.usda`, `load=LoadNone`（payload・全遅延、シェルのみ） | 0.017s | **24.6倍高速** |
| 上記 + 1/15棟だけを`stage.Load()`で追加ロード | 0.119s | **3.5倍高速** |

- `load=LoadAll`はベースラインとほぼ同じ時間になった。payload自体のオーバーヘッドは
  無視できるレベルで、常時ロードするなら`reference`と`payload`に実質差は無い
- `load=LoadNone`は、シェル（15個の空のXform + 未解決payload参照）だけを開くため、
  実データ146.5MBを一切パースせずに済み、大幅な短縮になった
- 「とりあえず1棟だけ表示してあとは必要に応じて読む」という現実的な使い方
  （`stage.Load(path)`で個別にロード）でも3.5倍の短縮が確認できた

## 考察・推奨

- **効果は明確**: 大規模モデルで「まず何か表示する」までの時間をpayloadで
  大幅に短縮できることを数値で確認した
- **ただし現時点でのifc2usd本体への実装は推奨しない**:
  1. 効果があるのは経路A（usdview/Omniverse/Blender）のみで、本命の経路B
     （Webビューワー）には無関係
  2. `build_stage()`を単一`.usda`から複数ファイル+payload構成へ変更するのは、
     `voxelize`/`export-gltf`/`serve`など「変換済みUSDは1ファイルに閉じている」
     ことを前提にしている既存の消費者すべてに影響する非小規模なアーキテクチャ
     変更になる
  3. 検証には実際の大規模IFCサンプルではなく、手元データを15倍に複製した模擬
     データを使っている。実際に大規模モデルの要望が出た時点で、実データでの
     再検証を行った上で実装要否を判断するのが妥当
- 将来、経路A向けに大規模モデル対応が必要になった場合は、Building単位や
  Storey単位でpayload化する設計（`docs/viewer/architecture.md`の経路Aの節に
  追記予定）を検討する

## 再現方法

計測に使ったスクリプトはこのリポジトリには含めていない（一時データ生成を伴う
使い捨てスクリプトのため）。同様の検証を行う場合は、上記「手法」の手順に従い、
`Usd.Stage.CreateNew()` + `GetReferences().AddReference()` /
`GetPayloads().AddPayload()`で2種類のルートレイヤーを作成し、
`Usd.Stage.Open(path, load=Usd.Stage.LoadNone)`と比較する。プロセスごとに
独立実行して計測することを忘れないこと（上記の理由）。
