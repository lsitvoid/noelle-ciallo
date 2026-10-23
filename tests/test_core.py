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
