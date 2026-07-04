"""IFC (building BIM) モデルを構造化された OpenUSD ステージへ変換するパッケージ。"""

__version__ = "0.2.0"

from .cli import convert

__all__ = ["convert"]
