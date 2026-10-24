"""核心单元测试。运行: python3 -m pytest tests/ -q

不依赖网络，不需要 API Key。
"""

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def tmp_home(monkeypatch):
    d = tempfile.mkdtemp(prefix="noelle-test-")
    monkeypatch.setenv("NOELLE_HOME", d)
    # 重置单例
    from noelle import storage
    storage._db = None
    yield d


# ---------- config ----------

def test_strip_jsonc():
    from noelle.config import strip_jsonc
    src = '{\n // 注释\n "a": 1, /* 块 */ "b": "//不是注释"\n}'
    assert json.loads(strip_jsonc(src)) == {"a": 1, "b": "//不是注释"}


def test_config_roundtrip():
    from noelle import config
    cfg = config.load()
    cfg["active_provider"] = "test"
    config.save(cfg)
    assert config.get("active_provider") == "test"
    assert config.get("tools.enabled") is True


def test_config_set_nested():
    from noelle import config
    config.set_value("display.expand_reasoning", True)
    assert config.get("display.expand_reasoning") is True


# ---------- storage ----------

def test_session_lifecycle():
    from noelle.storage import DB
    db = DB()
    sid = db.create_session("t1", mode="normal")
    assert db.get_session("t1")["id"] == sid
    db.add_message(sid, "user", "hi")
    db.add_message(sid, "assistant", "hello")
    assert db.message_count(sid) == 2
    assert db.pop_messages(sid, 1) == 1
    assert db.message_count(sid) == 1
    db.clear_session(sid)
    assert db.message_count(sid) == 0
    db.delete_session(sid)
    assert db.get_session("t1") is None


def test_session_by_index():
    from noelle.storage import DB
    db = DB()
    db.create_session("a")
    db.create_session("b")
    assert db.get_session("1") is not None


def test_memory():
    from noelle.storage import DB
    db = DB()
    db.add_memory("用户喜欢 Arch", persona="p1")
    db.add_memory("用户用 fish", persona="p1")
    assert len(db.search_memories("Arch", persona="p1")) == 1
    assert len(db.list_memories(persona="p1")) == 2
    assert db.clear_memories(persona="p1") == 2


def test_kb():
    from noelle.storage import DB
    db = DB()
    i = db.add_kb_doc("Btrfs", "/tmp/b.md", "btrfs 快照")
    assert db.search_kb("快照")[0]["title"] == "Btrfs"
    # 同路径再写是更新不是新增
    db.add_kb_doc("Btrfs 2", "/tmp/b.md", "新内容")
    assert len(db.list_kb()) == 1
    db.delete_kb_doc(i)
    assert len(db.list_kb()) == 0


def test_usage():
    from noelle.storage import DB
    db = DB()
    db.record_usage("p", "m", 10, 5)
    db.record_usage("p", "m", 20, 8)
    u = db.usage_summary()
    assert u["calls"] == 2
    assert u["prompt"] == 30
    assert u["completion"] == 13


# ---------- tools ----------

def test_tool_registry():
    from noelle.tools import registry
    assert "read_file" in registry.names()
    assert len(registry.schemas()) == len(registry.all())


def test_tool_read_write(tmp_path):
    from noelle.tools import registry
    f = tmp_path / "x.txt"
    f.write_text("line1\nline2\nline3\n")
    r = registry.call("read_file", {"path": str(f), "start": 2, "limit": 1})
    assert r.ok and "line2" in r.content

    registry.call("write_file", {"path": str(f), "content": "new\n"})
    assert f.read_text() == "new\n"

    registry.call("write_file", {"path": str(f), "content": "more\n", "mode": "append"})
    assert f.read_text() == "new\nmore\n"


def test_tool_edit(tmp_path):
    from noelle.tools import registry
    f = tmp_path / "y.txt"
    f.write_text("a b a\n")
    r = registry.call("edit_file", {"path": str(f), "old": "a", "new": "X"})
    assert not r.ok          # 多处匹配应拒绝
    r = registry.call("edit_file", {"path": str(f), "old": "a b", "new": "X"})
    assert r.ok and f.read_text() == "X a\n"


def test_tool_error_isolation():
    from noelle.tools import registry
    r = registry.call("read_file", {"path": "/nonexistent/xyz"})
    assert not r.ok and "不存在" in r.error
    r = registry.call("不存在的工具", {})
    assert not r.ok


def test_tool_unknown_arg():
    from noelle.tools import registry
    r = registry.call("now", {"bogus": 1})
    assert not r.ok


# ---------- shell classify ----------

@pytest.mark.parametrize("line,expect", [
    ("ls -la", "command"),
    ("sudo pacman -Syu", "command"),
    ("git status", "command"),
    ("/usr/bin/python3 x.py", "command"),
    ("echo hi | grep h", "command"),
    ("看看磁盘占用", "natural"),
    ("帮我写个脚本", "natural"),
    ("", "command"),
])
def test_classify(line, expect):
    from noelle.shell import classify
    assert classify(line) == expect


# ---------- llm ----------

def test_llm_payload_build():
    from noelle.llm import LLMClient, Message
    c = LLMClient("http://x/v1", "k", "m")
    p = c._payload([Message("user", "hi")], True, 0.5, 100, None)
    assert p["model"] == "m"
    assert p["stream"] is True
    assert p["messages"][0] == {"role": "user", "content": "hi"}


def test_llm_tool_schema():
    from noelle.tools import registry
    s = registry.schemas(["now"])
    assert s[0]["function"]["name"] == "now"


# ---------- paths ----------

def test_paths_isolated(tmp_home):
    from noelle import paths
    assert str(paths.home()) == tmp_home
    paths.ensure_dirs()
    assert paths.prompts_dir().is_dir()


# ---------- plugins ----------

def test_plugin_discover_and_toggle():
    from noelle.plugins import discover, set_enabled
    names = [d["name"] for d in discover()]
    assert "builtin:render" in names
    assert "builtin:webhook" in names
    assert set_enabled("render", False)
    states = {d["name"]: d["enabled"] for d in discover()}
    assert states["builtin:render"] is False
    assert set_enabled("render", True)
    states = {d["name"]: d["enabled"] for d in discover()}
    assert states["builtin:render"] is True


def test_plugin_toggle_unknown():
    from noelle.plugins import set_enabled
    assert not set_enabled("no-such-plugin", False)


def test_builtin_render_table():
    from noelle.plugins.builtin_plugins.render import parse_md_table
    r = parse_md_table("| a | b |\n|---|---|\n| 1 | 2 |")
    assert r is not None
    header, aligns, rows = r
    assert header == ["a", "b"]
    assert rows == [["1", "2"]]
    assert parse_md_table("不是表格") is None


def test_builtin_render_align():
    from noelle.plugins.builtin_plugins.render import parse_md_table
    _, aligns, _ = parse_md_table("| a | b | c |\n|:--|:-:|--:|\n| 1 | 2 | 3 |")
    assert aligns == ["left", "center", "right"]


def test_builtin_render_code_typst():
    from noelle.plugins.builtin_plugins.render import code_to_typst
    src = code_to_typst("x=1", "python")
    assert "```python" in src


def test_webhook_tools_registered():
    from noelle.plugins.builtin_plugins import webhook
    from noelle.tools import ToolRegistry
    reg = ToolRegistry()
    webhook.setup(reg)
    assert "bridge_hint" in reg.names()
    assert "bridge_status" in reg.names()


# ---------- tapall / 会话恢复 ----------

def test_tapall_lists_and_recovers(tmp_home, capsys):
    from noelle import cli
    from noelle.storage import get_db
    db = get_db()
    sid = db.create_session("测试对话", mode="normal")
    db.add_message(sid, "user", "你好")
    db.add_message(sid, "assistant", "你好呀")

    args = cli.build_parser().parse_args(["tapall"])
    assert cli.cmd_tapall(args) == 0
    text = capsys.readouterr().out
    assert "测试对话" in text and "2" in text

    # 序号 1 → 恢复该会话进 REPL（不实际跑 LLM，仅验证路径）
    args2 = cli.build_parser().parse_args(["tapall", "99"])
    assert cli.cmd_tapall(args2) == 1


def test_auto_session_persists(tmp_home):
    from noelle import cli
    from noelle.storage import get_db
    sid = cli._auto_session({}, "ask")
    db = get_db()
    row = db.get_session(sid)
    assert row is not None and row["name"].startswith("ask-")
    assert row["mode"] == "ask"


# ---------- 关键记忆三类 ----------

def test_memory_kinds(tmp_home):
    from noelle.storage import get_db
    db = get_db()
    db.add_memory_typed("喜欢简短回答", "", "user", "auto")
    db.add_memory_typed("被讨厌时该收敛", "", "self", "auto")
    db.add_memory_typed("杭州比赛第一名", "", "event", "auto")
    assert len(db.memories_by_kind("user")) == 1
    assert len(db.memories_by_kind("self")) == 1
    assert len(db.memories_by_kind("event")) == 1


def test_event_recall(tmp_home):
    from noelle.agent import Agent
    from noelle.storage import get_db
    db = get_db()
    db.add_memory_typed("上周末杭州比赛拿了第一名的奖杯", "", "event", "auto")
    a = Agent({"active_provider": "zhipu"})
    blk = a.recall_block("我那个杭州比赛后来怎么样了")
    assert "杭州比赛" in blk
    assert a.recall_block("今天天气如何") == ""


def test_profile_block(tmp_home):
    from noelle.agent import Agent
    from noelle.storage import get_db
    db = get_db()
    db.add_memory_typed("用户叫小明", "", "user", "auto")
    db.add_memory_typed("回答要简短", "", "self", "auto")
    a = Agent({})
    blk = a.profile_block()
    assert "关于用户" in blk and "小明" in blk
    assert "关于你自己" in blk and "简短" in blk
