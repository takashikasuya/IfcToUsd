# IFC2USD
IFCをUSDに変換するプログラム。

## Preparation
1. conda環境の新規立ち上げ。IfcOpenShellをインストールする必要があるため。
2. USDの関連ライブラリをbuildする。この際、上記環境をactivateするとよい。[参考](https://fereria.github.io/reincarnation_tech/11_Pipeline/01_USD/00_install_USD/)
3. 依存ライブラリのインストール

## Requirements
- python >= 3.8
- [usd](https://github.com/PixarAnimationStudios/USD)
- [ifcopenshell](https://github.com/IfcOpenShell/IfcOpenShell)
- [tqdm](https://tqdm.github.io/)