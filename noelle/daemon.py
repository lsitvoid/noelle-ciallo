"""后台服务：Unix socket / TCP 承载 JSON 行协议。

客户端发送一行 JSON 请求，服务端回一行 JSON 事件。
这样前端（TUI / WebUI / 编辑器插件）都能复用同一个会话状态。

Windows 无 AF_UNIX 时回退到 127.0.0.1 TCP。
"""

from __future__ import annotations

import json
import os
import socket
import socketserver
import sys
import threading
import time
from pathlib import Path

from . import paths
from .agent import Agent
from .storage import get_db


def socket_path() -> Path:
    return paths.runtime_dir() / "daemon.sock"


def pid_file() -> Path:
    return paths.runtime_dir() / "daemon.pid"


class Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        while True:
            line = self.rfile.readline()
            if not line:
                break
            try:
                req = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError:
                self._send({"event": "error", "message": "JSON 解析失败"})
                continue

            try:
                self._dispatch(req)
            except Exception as e:  # noqa: BLE001
                self._send({"event": "error",
                            "message": f"{type(e).__name__}: {e}"})

    def _send(self, obj: dict) -> None:
        self.wfile.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        self.wfile.flush()

    def _dispatch(self, req: dict) -> None:
        op = req.get("op", "")

        if op == "ping":
            self._send({"event": "pong", "time": time.time()})
            return

        if op == "ask":
            agent = Agent()
            text = req.get("text", "")
            sid = req.get("session", "")

            def on_event(ev: tuple[str, str]) -> None:
                kind, payload = ev
                if kind == "text":
                    self._send({"event": "text", "text": payload})
                elif kind == "tool":
                    self._send({"event": "tool", "name": payload})
                elif kind == "error":
                    self._send({"event": "error", "message": payload})

            if sid:
                turn = agent.chat(sid, text, stream=True, on_event=on_event)
            else:
                turn = agent.run(text, stream=True, on_event=on_event)
            self._send({
                "event": "done",
                "tokens": [turn.prompt_tokens, turn.completion_tokens],
            })
            return

        if op == "sessions":
            db = get_db()
            self._send({
                "event": "sessions",
                "data": [dict(r) for r in db.list_sessions()],
            })
            return

        self._send({"event": "error", "message": f"未知操作: {op}"})


class UnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


class TCPServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def run(host: str = "127.0.0.1", port: int = 0) -> None:
    paths.ensure_dirs()
    sp = socket_path()
    use_unix = hasattr(socket, "AF_UNIX") and os.name != "nt"

    if use_unix:
        if sp.exists():
            sp.unlink()
        server = UnixServer(str(sp), Handler)
        sp.chmod(0o600)
        info = str(sp)
    else:
        server = TCPServer((host, port or 7788), Handler)
        info = f"tcp://{host}:{server.server_address[1]}"

    pid_file().write_text(str(os.getpid()), encoding="utf-8")
    print(f"[noelle daemon] listening on {info}", file=sys.stderr, flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if use_unix and sp.exists():
            sp.unlink()
        if pid_file().exists():
            pid_file().unlink()


def client_connect(timeout: float = 5.0) -> socket.socket:
    sp = socket_path()
    if hasattr(socket, "AF_UNIX") and os.name != "nt" and sp.exists():
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(str(sp))
        return s
    s = socket.create_connection(("127.0.0.1", 7788), timeout=timeout)
    return s


def send(req: dict) -> list[dict]:
    """发一个请求，收所有事件直到 done"""
    events: list[dict] = []
    with client_connect() as s:
        s.sendall((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
        buf = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                obj = json.loads(line.decode("utf-8"))
                events.append(obj)
                if obj.get("event") in ("done", "pong", "error", "sessions"):
                    return events
    return events


def status() -> dict:
    pf = pid_file()
    if not pf.is_file():
        return {"running": False}
    try:
        pid = int(pf.read_text().strip())
    except (ValueError, OSError):
        return {"running": False}
    try:
        os.kill(pid, 0)
    except OSError:
        return {"running": False, "stale_pid": pid}
    return {"running": True, "pid": pid, "socket": str(socket_path())}


def stop() -> bool:
    st = status()
    if not st.get("running"):
        return False
    try:
        os.kill(int(st["pid"]), 15)
        for _ in range(30):
            time.sleep(0.1)
            if not status().get("running"):
                break
        return True
    except OSError:
        return False


def main() -> None:
    run()


if __name__ == "__main__":
    main()
