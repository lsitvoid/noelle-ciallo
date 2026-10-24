"""SQLite 持久层：会话、消息、记忆、知识库元数据。

只用标准库 sqlite3。开启 WAL 以支持后台 daemon 与前台 CLI 并发读。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

from .. import paths

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    mode        TEXT NOT NULL DEFAULT 'normal',
    persona     TEXT NOT NULL DEFAULT '',
    provider    TEXT NOT NULL DEFAULT '',
    model       TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    archived    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    reasoning   TEXT NOT NULL DEFAULT '',
    tool_calls  TEXT NOT NULL DEFAULT '',
    tokens      INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);

CREATE TABLE IF NOT EXISTS memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    persona     TEXT NOT NULL DEFAULT '',
    content     TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memories_persona ON memories(persona);

CREATE TABLE IF NOT EXISTS kb_docs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    path        TEXT NOT NULL,
    content     TEXT NOT NULL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kb_title ON kb_docs(title);

CREATE TABLE IF NOT EXISTS usage (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    provider    TEXT NOT NULL DEFAULT '',
    model       TEXT NOT NULL DEFAULT '',
    prompt      INTEGER NOT NULL DEFAULT 0,
    completion  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage(ts DESC);
"""


class DB:
    def __init__(self, path: Path | None = None):
        paths.ensure_dirs()
        self.path = path or paths.db_file()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        with self.conn() as c:
            c.executescript(SCHEMA)

    # ---------- 连接 ----------
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            # isolation_level=None: autocommit，任何异常都不会留下
            # 悬置写事务（多线程 bridge 的"database is locked"根因）
            c = sqlite3.connect(
                self.path, timeout=10, check_same_thread=False,
                isolation_level=None,
            )
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA busy_timeout=10000")
            self._local.conn = c
        return c

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None

    # ---------- 会话 ----------
    def create_session(
        self, name: str, mode: str = "normal", persona: str = "",
        provider: str = "", model: str = "",
    ) -> str:
        sid = uuid.uuid4().hex[:16]
        now = time.time()
        self.conn().execute(
            "INSERT INTO sessions(id,name,mode,persona,provider,model,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (sid, name, mode, persona, provider, model, now, now),
        )
        self.conn().commit()
        return sid

    def get_session(self, key: str) -> sqlite3.Row | None:
        """按 id / name / 序号查找"""
        c = self.conn()
        row = c.execute("SELECT * FROM sessions WHERE id=? OR name=? ORDER BY updated_at DESC",
                        (key, key)).fetchone()
        if row:
            return row
        if key.isdigit():
            rows = c.execute(
                "SELECT * FROM sessions WHERE archived=0 ORDER BY updated_at DESC"
            ).fetchall()
            idx = int(key) - 1
            if 0 <= idx < len(rows):
                return rows[idx]
        return None

    def list_sessions(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.conn().execute(
            "SELECT s.*, (SELECT COUNT(*) FROM messages m WHERE m.session_id=s.id) AS msgs"
            " FROM sessions s WHERE archived=0 ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def touch_session(self, sid: str) -> None:
        self.conn().execute("UPDATE sessions SET updated_at=? WHERE id=?", (time.time(), sid))
        self.conn().commit()

    def rename_session(self, sid: str, name: str) -> None:
        self.conn().execute("UPDATE sessions SET name=? WHERE id=?", (name, sid))
        self.conn().commit()

    def delete_session(self, sid: str) -> None:
        self.conn().execute("DELETE FROM sessions WHERE id=?", (sid,))
        self.conn().commit()

    def clear_session(self, sid: str) -> None:
        self.conn().execute("DELETE FROM messages WHERE session_id=?", (sid,))
        self.conn().commit()

    def update_session(self, sid: str, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn().execute(
            f"UPDATE sessions SET {cols} WHERE id=?", (*fields.values(), sid)
        )
        self.conn().commit()

    # ---------- 消息 ----------
    def add_message(
        self, session_id: str, role: str, content: str,
        reasoning: str = "", tool_calls: Any = None, tokens: int = 0,
    ) -> int:
        tc = json.dumps(tool_calls, ensure_ascii=False) if tool_calls else ""
        cur = self.conn().execute(
            "INSERT INTO messages(session_id,role,content,reasoning,tool_calls,tokens,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (session_id, role, content, reasoning, tc, tokens, time.time()),
        )
        self.conn().commit()
        self.touch_session(session_id)
        return int(cur.lastrowid or 0)

    def messages(self, session_id: str, limit: int = 0) -> list[sqlite3.Row]:
        sql = "SELECT * FROM messages WHERE session_id=? ORDER BY id"
        params: tuple = (session_id,)
        if limit > 0:
            sql = (
                "SELECT * FROM (SELECT * FROM messages WHERE session_id=?"
                " ORDER BY id DESC LIMIT ?) ORDER BY id"
            )
            params = (session_id, limit)
        return self.conn().execute(sql, params).fetchall()

    def pop_messages(self, session_id: str, count: int = 1) -> int:
        c = self.conn()
        ids = [r["id"] for r in c.execute(
            "SELECT id FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
            (session_id, count),
        ).fetchall()]
        if ids:
            c.executemany("DELETE FROM messages WHERE id=?", [(i,) for i in ids])
            c.commit()
        return len(ids)

    def message_count(self, session_id: str) -> int:
        r = self.conn().execute(
            "SELECT COUNT(*) AS n FROM messages WHERE session_id=?", (session_id,)
        ).fetchone()
        return int(r["n"]) if r else 0

    # ---------- 记忆 ----------
    def add_memory(self, content: str, persona: str = "", source: str = "") -> int:
        cur = self.conn().execute(
            "INSERT INTO memories(persona,content,source,created_at) VALUES(?,?,?,?)",
            (persona, content, source, time.time()),
        )
        self.conn().commit()
        return int(cur.lastrowid or 0)

    def search_memories(self, query: str, persona: str = "", limit: int = 20) -> list[sqlite3.Row]:
        like = f"%{query}%"
        return self.conn().execute(
            "SELECT * FROM memories WHERE content LIKE ? AND (?='' OR persona=?)"
            " ORDER BY id DESC LIMIT ?",
            (like, persona, persona, limit),
        ).fetchall()

    def list_memories(self, persona: str = "", limit: int = 50) -> list[sqlite3.Row]:
        return self.conn().execute(
            "SELECT * FROM memories WHERE (?='' OR persona=?) ORDER BY id DESC LIMIT ?",
            (persona, persona, limit),
        ).fetchall()

    def delete_memory(self, mem_id: int) -> None:
        self.conn().execute("DELETE FROM memories WHERE id=?", (mem_id,))
        self.conn().commit()

    def clear_memories(self, persona: str = "") -> int:
        c = self.conn()
        if persona:
            cur = c.execute("DELETE FROM memories WHERE persona=?", (persona,))
        else:
            cur = c.execute("DELETE FROM memories")
        c.commit()
        return cur.rowcount

    # ---------- 知识库 ----------
    def add_kb_doc(self, title: str, path: str, content: str) -> int:
        now = time.time()
        c = self.conn()
        row = c.execute("SELECT id FROM kb_docs WHERE path=?", (path,)).fetchone()
        if row:
            c.execute(
                "UPDATE kb_docs SET title=?,content=?,updated_at=? WHERE id=?",
                (title, content, now, row["id"]),
            )
            c.commit()
            return int(row["id"])
        cur = c.execute(
            "INSERT INTO kb_docs(title,path,content,created_at,updated_at) VALUES(?,?,?,?,?)",
            (title, path, content, now, now),
        )
        c.commit()
        return int(cur.lastrowid or 0)

    def search_kb(self, query: str, limit: int = 10) -> list[sqlite3.Row]:
        return self.conn().execute(
            "SELECT * FROM kb_docs WHERE content LIKE ? OR title LIKE ? LIMIT ?",
            (f"%{query}%", f"%{query}%", limit),
        ).fetchall()

    def list_kb(self, limit: int = 100) -> list[sqlite3.Row]:
        return self.conn().execute(
            "SELECT id,title,path,updated_at FROM kb_docs ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def delete_kb_doc(self, doc_id: int) -> None:
        self.conn().execute("DELETE FROM kb_docs WHERE id=?", (doc_id,))
        self.conn().commit()

    # ---------- 用量 ----------
    def record_usage(self, provider: str, model: str, prompt: int, completion: int) -> None:
        self.conn().execute(
            "INSERT INTO usage(ts,provider,model,prompt,completion) VALUES(?,?,?,?,?)",
            (time.time(), provider, model, prompt, completion),
        )
        self.conn().commit()

    def usage_summary(self, since: float = 0.0) -> sqlite3.Row:
        return self.conn().execute(
            "SELECT COUNT(*) AS calls, COALESCE(SUM(prompt),0) AS prompt,"
            " COALESCE(SUM(completion),0) AS completion FROM usage WHERE ts>=?",
            (since,),
        ).fetchone()

    # ---------- 维护 ----------
    def wipe(self) -> None:
        c = self.conn()
        for t in ("messages", "sessions", "memories", "usage"):
            c.execute(f"DELETE FROM {t}")
        c.commit()

    def vacuum(self) -> None:
        self.conn().execute("VACUUM")
        self.conn().commit()


_db: DB | None = None


def get_db() -> DB:
    global _db
    if _db is None:
        _db = DB()
    return _db


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]
