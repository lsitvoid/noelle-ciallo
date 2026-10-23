"""判断一行输入是 shell 命令还是自然语言。

规则（按优先级）：
  1. 空行、纯空白 → 命令（交给 shell）
  2. 含 shell 运算符 → 命令
  3. 首词是已知命令 / 路径 → 命令
  4. 含 CJK 字符 → 自然语言
  5. "word -flag" 形式 → 命令
  6. 其余 → 自然语言
"""

from __future__ import annotations

import re
import shutil

SHELL_OPERATORS = re.compile(r"[|&;<>$`]|&&|\|\||>>|<<")
CJK = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")
BUILTINS = {
    "cd", "export", "alias", "source", ".", "set", "unset",
    "sudo", "doas", "time", "nohup", "exec", "eval", "exit",
    "pushd", "popd", "fg", "bg", "jobs", "history", "which",
}


def _known_command(word: str) -> bool:
    if not word:
        return False
    if word in BUILTINS:
        return True
    if word.startswith(("./", "../", "/", "~/")):
        return True
    return shutil.which(word) is not None


def classify(line: str) -> str:
    """返回 'command' 或 'natural'"""
    s = line.strip()
    if not s:
        return "command"

    if SHELL_OPERATORS.search(s):
        return "command"

    first = s.split(maxsplit=1)[0]
    if _known_command(first):
        return "command"

    if CJK.search(s):
        return "natural"

    if re.match(r"^[\w./\-]+(\s+-{1,2}[\w\-]+)+$", s):
        return "command"

    return "natural"


def should_intercept(line: str) -> bool:
    return classify(line) == "natural"
