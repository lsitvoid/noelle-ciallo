"""shell 集成：fish / bash / zsh 的 hook 生成与自然语言分类。"""

from .classify import classify
from .hooks import generate

__all__ = ["classify", "generate"]
