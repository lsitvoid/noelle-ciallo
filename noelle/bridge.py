"""webhook 桥：让外部聊天软件接入 noelle。

    POST /message  {"text": "...", "from": "user-id"}
         → 200     {"reply": "...", "session": "..."}

由 `noelle bridge [--port P]` 前台常驻，或 daemon start 时自动挂载。
平台适配器（Telegram bot / QQ 机器人 / 企业微信…）只需把收到的
消息 POST 进来，把 reply 发回去。
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class BridgeHandler(BaseHTTPRequestHandler):
    agent = None          # 启动时注入
    sessions: dict = {}   # from → session_id

    def log_message(self, *a):
        pass

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"error": "JSON 解析失败"})

        if self.path != "/message":
            return self._json(404, {"error": f"未知路径 {self.path}"})

        text = (req.get("text") or "").strip()
        sender = str(req.get("from") or "anon")
        if not text:
            return self._json(400, {"error": "text 为空"})

        sid = BridgeHandler.sessions.setdefault(sender, f"bridge-{sender}")
        try:
            turn = BridgeHandler.agent.chat(sid, text, stream=False)
            self._json(200, {"reply": turn.text, "session": sid})
        except Exception as e:  # noqa: BLE001
            self._json(502, {"error": f"{type(e).__name__}: {e}"})


def make_server(port: int = 7800):
    from .agent import Agent
    BridgeHandler.agent = Agent()
    return ThreadingHTTPServer(("0.0.0.0", port), BridgeHandler)


def serve(port: int = 7800) -> None:
    srv = make_server(port)
    print(f"[noelle bridge] listening on http://0.0.0.0:{srv.server_address[1]}",
          flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
