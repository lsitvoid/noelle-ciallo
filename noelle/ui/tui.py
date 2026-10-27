"""noelle 独立终端 UI（REPL）—— 交互式 TUI 页面。

照搬 miyu 终端体验：标题栏 / 流式输出后 Markdown 重渲染 /
斜杠命令 / Ctrl+C 中断本轮 / 心情与模型状态。纯标准库。
"""

from __future__ import annotations

import os
import re
import signal
import sys

from .term import C, banner, out


# ============================================================
# Markdown → ANSI
# ============================================================
_RE_CODEBLOCK = re.compile(r"```(\w*)\n(.*?)```", re.S)
_RE_INLINE = re.compile(r"`([^`]+)`")
_RE_BOLD = re.compile(r"\*\*(.+?)\*\*")
_RE_ITALIC = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_RE_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_RE_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_RE_QUOTE = re.compile(r"^>\s?(.*)$")
_RE_LIST = re.compile(r"^(\s*)[-*]\s+(.*)$")


def _inline(s: str) -> str:
    s = _RE_INLINE.sub("\033[36m\\1\033[0m", s)
    s = _RE_BOLD.sub("\033[1m\\1\033[0m", s)
    s = _RE_ITALIC.sub("\033[3m\\1\033[0m", s)
    s = _RE_LINK.sub("\\1 \033[2m(\\2)\033[0m", s)
    return s


def _codeblock(m: "re.Match[str]") -> str:
    lang, code = m.group(1), m.group(2).rstrip("\n")
    body = "\n".join("  \033[36m" + ln + "\033[0m" for ln in code.split("\n"))
    head = "\033[2m  ┌─ " + (lang or "code") + " " + "─" * 8 + "\033[0m"
    foot = "\033[2m  └" + "─" * 16 + "\033[0m"
    return "\n".join([head, body, foot])


def md2ansi(text: str) -> str:
    """把 LLM 输出的 Markdown 粗渲染成终端 ANSI。"""
    text = _RE_CODEBLOCK.sub(_codeblock, text)
    blocks = []
    for line in text.split("\n"):
        h = _RE_HEADING.match(line)
        if h:
            level = len(h.group(1))
            body = "\033[1m" + _inline(h.group(2)) + "\033[0m"
            line = ("\n" + body) if level <= 2 else body
        else:
            q = _RE_QUOTE.match(line)
            li = _RE_LIST.match(line)
            if q:
                line = "\033[2m│ \033[0m" + _inline(q.group(1))
            elif li:
                indent = " " * (len(li.group(1)) // 2 * 2)
                line = indent + "\033[35m•\033[0m " + _inline(li.group(2))
            else:
                line = _inline(line)
        blocks.append(line)
    return "\n".join(blocks)


def _term_width() -> int:
    try:
        return os.get_terminal_size().columns
    except OSError:
        return 80


def hr(char: str = "─") -> str:
    return "\033[2m" + char * min(_term_width(), 80) + "\033[0m"


# ============================================================
# REPL
# ============================================================
SLASH_HELP = [
    ("/help", "命令列表"),
    ("/exit", "退出（Ctrl+D 同）"),
    ("/reset", "清空当前会话上下文"),
    ("/history", "最近 10 条消息"),
    ("/model", "当前模型 / 列出可用模型"),
    ("/mood", "查看心情状态"),
    ("/tools", "列出已启用工具"),
    ("/persona", "当前人格"),
    ("/reasoning", "切换思考过程显示"),
    ("/usage", "本会话消息统计"),
    ("/config", "配置文件路径"),
    ("/clear", "清屏"),
]


class Tui:
    """REPL 界面：持有 Agent 与当前会话 id。"""

    def __init__(self, agent, db, sid: str, name: str):
        self.a = agent
        self.db = db
        self.sid = sid
        self.name = name
        try:
            self.pid, self.model, _ = agent.resolve()
        except Exception:
            self.pid, self.model = "未配置", ""
        self.show_reasoning = bool(
            agent.cfg.get("display", {}).get("expand_reasoning", False))

    # ---------- 界面元素 ----------
    def header(self) -> None:
        title = "\033[1m\033[7m noelle \033[0m\033[1m"
        info = f" {self.pid}/{self.model} · {self.name}\033[0m"
        out(title + info)
        out(hr())

    def welcome(self) -> None:
        banner()
        self.header()
        out("\033[2m自然语言直接说 · /help 看命令 · Ctrl+C 中断回答 · Ctrl+D 退出\033[0m")
        out("")

    # ---------- 主循环 ----------
    def run(self) -> int:
        self.welcome()
        from .term import prompt_input
        while True:
            try:
                line = prompt_input("\033[32m❯ \033[0m").strip()
            except (EOFError, SystemExit):
                out("\033[2m再见～\033[0m")
                break
            except KeyboardInterrupt:
                out("")
                continue
            if not line:
                continue
            if line.startswith("/"):
                if self.slash(line):
                    out("\033[2m再见～\033[0m")
                    break
                continue
            self.chat_round(line)
            out("")

    # ---------- 一轮对话 ----------
    def chat_round(self, text: str) -> None:
        state = {"show": self.show_reasoning}

        def on_event(ev) -> None:
            kind, payload = ev
            if kind == "text":
                sys.stdout.write(payload)
                sys.stdout.flush()
            elif kind == "reasoning" and state["show"]:
                sys.stdout.write("\033[2m" + payload + "\033[0m")
                sys.stdout.flush()
            elif kind == "error":
                sys.stdout.write("\033[31m\n[错误] " + payload + "\033[0m")
                sys.stdout.flush()

        old = signal.getsignal(signal.SIGINT)
        stop = {"hit": False}

        def on_int(sig, frm):
            stop["hit"] = True
            sys.stdout.write("\n\033[2m（已中断本轮回答）\033[0m\n")
            sys.stdout.flush()
            raise KeyboardInterrupt

        signal.signal(signal.SIGINT, on_int)
        interrupted = False
        turn = None
        try:
            turn = self.a.chat(self.sid, text, stream=True, on_event=on_event)
        except KeyboardInterrupt:
            interrupted = True
        except Exception as e:
            out("\033[31m[错误] " + str(e) + "\033[0m")
        finally:
            signal.signal(signal.SIGINT, old)

        # 流式原文输出后，用 Markdown 重渲染完整回复（换行排版）
        if turn is not None and getattr(turn, "text", "") and not interrupted:
            sys.stdout.write("\n")
            sys.stdout.flush()
            out(md2ansi(turn.text))
        out(hr())

    # ---------- 斜杠命令 ----------
    def slash(self, line: str) -> int:
        """返回 1 表示退出。"""
        cmd, *rest = line.split(maxsplit=1)
        if cmd in ("/exit", "/quit", "/q"):
            return 1
        if cmd in ("/help", "/?"):
            out("\033[1m斜杠命令\033[0m")
            for c, d in SLASH_HELP:
                out(f"  \033[36m{c:<12}\033[0m {d}")
            out("")
        elif cmd == "/reset":
            self.db.clear_session(self.sid)
            out("\033[2m上下文已清空\033[0m\n")
        elif cmd == "/history":
            for r in self.db.messages(self.sid)[-10:]:
                who = "你  " if r["role"] == "user" else "noelle"
                out("\033[2m" + who + "\033[0m " +
                    r["content"][:120].replace("\n", " "))
            out("")
        elif cmd == "/model":
            out(f"当前：\033[36m{self.pid}/{self.model}\033[0m")
            if os.system("noelle list-models 2>/dev/null | head -15") != 0:
                out("(list-models 不可用)")
            out("")
        elif cmd == "/mood":
            try:
                from ..mood import current_state
                out(f"心情：\033[35m{current_state(self.db)}\033[0m / 100\n")
            except Exception as e:
                out(f"(心情系统未就绪: {e})\n")
        elif cmd == "/tools":
            try:
                from ..tools import tool_registry
                out(" ".join(sorted(tool_registry.names())) + "\n")
            except Exception as e:
                out(f"(工具注册表不可用: {e})\n")
        elif cmd == "/persona":
            p = self.a.cfg.get("prompt", {}).get("active_persona", "")
            out(f"人格：\033[36m{p or '(默认)'}\033[0m\n")
        elif cmd == "/reasoning":
            self.show_reasoning = not self.show_reasoning
            state = "开" if self.show_reasoning else "关"
            out(f"\033[2m思考过程显示：{state}\033[0m\n")
        elif cmd == "/usage":
            n = len(self.db.messages(self.sid))
            out(f"\033[2m本会话共 {n} 条消息\033[0m\n")
        elif cmd == "/config":
            from .. import paths
            out(str(paths.config_file()) + "\n")
        elif cmd == "/clear":
            os.system(self._clear_cmd())
            self.welcome()
        else:
            out(f"\033[2m未知命令 {cmd}，/help 查看列表\033[0m\n")
        return 0

    @staticmethod
    def _clear_cmd() -> str:
        return "cls" if os.name == "nt" else "clear"
