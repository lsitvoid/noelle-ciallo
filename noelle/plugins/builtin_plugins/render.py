"""render 插件：把 markdown 表格 / 代码块渲染成 PNG。

方法借鉴 max（HCHogan/max）的 Max.Render：
  · markdown 表格 → typst 源码 → `typst compile --format png --ppi 144`
  · 代码块 → typst 源码（带行号 + 等宽字体）→ PNG

输出到 ~/.noelle/cache/render/，终端里用 chafa 预览。

依赖：系统装有 typst（extra 仓库）。没有时插件自动降级为提示。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from noelle import paths
from noelle.tools import Tool, registry


def _typst() -> str | None:
    return shutil.which("typst")


def _compile(typst_src: str, timeout: int = 20) -> Path:
    """typst 源码 → PNG 路径。失败抛 RuntimeError。"""
    out_dir = paths.cache_dir() / "render"
    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"{int(time.time() * 1000)}.png"
    out = out_dir / name
    with tempfile.TemporaryDirectory(prefix="noelle-render-") as td:
        src = Path(td) / "in.typ"
        src.write_text(typst_src, encoding="utf-8")
        try:
            r = subprocess.run(
                ["typst", "compile", "--format", "png", "--ppi", "144",
                 str(src), str(out)],
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("typst 超时") from None
        if r.returncode != 0:
            raise RuntimeError(f"typst 失败: {r.stderr.strip()[:500]}")
    return out


def _wrap_page(body: str, width_cm: float = 16.0) -> str:
    return (
        "#set page(width: "
        f"{width_cm}cm, height: auto, margin: 0.6cm, fill: white)\n"
        "#set text(font: (\"Noto Sans CJK SC\", \"Source Han Sans SC\",), size: 10.5pt)\n"
        + body
    )


# ---------- markdown 表格解析 ----------

TABLE_ROW = re.compile(r"^\s*\|(.+)\|\s*$")


def parse_md_table(src: str) -> tuple[list[str], list[str], list[list[str]]] | None:
    """返回 (表头, 对齐, 行)。不是合法表格返回 None。"""
    lines = [ln for ln in src.strip().splitlines() if ln.strip()]
    if len(lines) < 2:
        return None
    rows = []
    for ln in lines:
        m = TABLE_ROW.match(ln)
        if not m:
            return None
        rows.append([c.strip() for c in m.group(1).split("|")])
    header, sep, body = rows[0], rows[1], rows[2:]
    if len(sep) < 2 or not all(re.fullmatch(r":?-+:?", c) for c in sep):
        return None
    aligns = []
    for c in sep:
        l, r = c.startswith(":"), c.endswith(":")
        aligns.append("center" if l and r else "right" if r else "left" if l else "auto")
    ncol = len(header)
    aligns = (aligns + ["auto"] * ncol)[:ncol]
    body = [r + [""] * (ncol - len(r)) if len(r) < ncol else r[:ncol] for r in body]
    return header, aligns, body


def table_to_typst(header: list[str], aligns: list[str], rows: list[list[str]]) -> str:
    cols = ", ".join(aligns)
    out = [f"#table(columns: ({cols}), inset: 6pt, stroke: 0.5pt)"]
    for cell in header:
        out.append(f"  table.header([**{_esc(cell)}**]),")
    for row in rows:
        for cell in row:
            out.append(f"  [{_esc(cell)}],")
    out.append(")")
    return "\n".join(out)


def _esc(s: str) -> str:
    return s.replace("#", "\\#").replace("$", "\\$").replace("*", "\\*")


# ---------- 代码块渲染 ----------

def code_to_typst(code: str, lang: str = "") -> str:
    lang = re.sub(r"[^a-zA-Z0-9_+-]", "", lang)
    raw_block = "```" + lang + "\n" + code + "\n```"
    return _wrap_page(
        "#set text(font: (\"JetBrains Mono\", \"Noto Sans Mono\", \"monospace\",), size: 9pt)\n"
        + raw_block,
        width_cm=14.0,
    )


# ---------- 工具 ----------

class RenderTableTool(Tool):
    name = "render_table"
    description = (
        "把 markdown 表格渲染成 PNG 图片，返回文件路径并已在终端预览。"
        "适合展示对比数据。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "table": {"type": "string", "description": "markdown 表格源码"},
            "show": {"type": "boolean", "description": "是否在终端预览，默认 true"},
        },
        "required": ["table"],
    }

    def run(self, table: str, show: bool = True) -> str:
        if not _typst():
            return "[render] 未安装 typst：sudo pacman -S typst"
        parsed = parse_md_table(table)
        if not parsed:
            return "[render] 不是合法的 markdown 表格"
        header, aligns, rows = parsed
        src = _wrap_page(table_to_typst(header, aligns, rows))
        png = _compile(src)
        return _finish(png, show)


class RenderCodeTool(Tool):
    name = "render_code"
    description = (
        "把代码片段渲染成带高亮的 PNG 图片（代码截图风格），"
        "返回文件路径并已在终端预览。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "code": {"type": "string"},
            "lang": {"type": "string", "description": "语言标注，可空"},
            "show": {"type": "boolean"},
        },
        "required": ["code"],
    }

    def run(self, code: str, lang: str = "", show: bool = True) -> str:
        if not _typst():
            return "[render] 未安装 typst：sudo pacman -S typst"
        png = _compile(code_to_typst(code, lang))
        return _finish(png, show)


def _finish(png: Path, show: bool) -> str:
    note = f"已生成 {png}"
    if show and shutil.which("chafa"):
        try:
            subprocess.run(["chafa", "--size", "72x36", str(png)], timeout=10)
        except subprocess.TimeoutExpired:
            pass
    return f"{note}\n查看: chafa {png}"


class RenderReplyTool(Tool):
    """一次性渲染整段回复：抽出的表格与代码块各生成一张图。"""

    name = "render_reply"
    description = (
        "扫描一段含 markdown 表格/代码块的文本，把每个表格和代码块"
        "分别渲染成 PNG。返回生成的文件列表。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "含 markdown 的文本"},
            "show": {"type": "boolean"},
        },
        "required": ["text"],
    }

    def run(self, text: str, show: bool = False) -> str:
        if not _typst():
            return "[render] 未安装 typst：sudo pacman -S typst"
        results: list[str] = []
        # 代码块
        for m in re.finditer(r"```([\w+#.-]*)\n(.*?)```", text, re.S):
            lang, code = m.group(1), m.group(2)
            if not code.strip():
                continue
            png = _compile(code_to_typst(code, lang))
            results.append(f"code({lang or 'text'}) → {png}")
        # 表格：连续行中含 | 的块
        lines, buf = text.splitlines(), []

        def flush():
            if buf:
                block = "\n".join(buf)
                parsed = parse_md_table(block)
                if parsed:
                    png = _compile(_wrap_page(table_to_typst(*parsed)))
                    results.append(f"table → {png}")
                buf.clear()

        for ln in lines:
            if "|" in ln and ln.strip():
                buf.append(ln)
            else:
                flush()
        flush()

        if not results:
            return "[render] 没有可渲染的表格或代码块"
        out = "\n".join(results)
        if show:
            for r in results:
                p = r.split("→ ")[-1]
                subprocess.run(["chafa", "--size", "72x36", p], timeout=10)
        return out


def setup(reg) -> None:
    reg.register(RenderTableTool)
    reg.register(RenderCodeTool)
    reg.register(RenderReplyTool)
