"""webhook bridge 插件：把 noelle 接入任意聊天软件。

服务本体在 noelle/bridge.py，由 `noelle bridge [--port P]` 常驻
（tmux / systemd）。本插件给对话中的模型提供桥的状态查询与指引。

    noelle bridge --port 7800      # 前台常驻
    curl -X POST localhost:7800/message \
         -d '{"text":"你好","from":"u1"}'
"""

from __future__ import annotations

from noelle.tools import Tool, registry


class BridgeHintTool(Tool):
    name = "bridge_hint"
    description = "返回 webhook 桥的启动与接入方法"
    parameters = {"type": "object", "properties": {}}

    def run(self) -> str:
        return (
            "启动服务: noelle bridge --port 7800\n"
            "发消息: curl -X POST http://127.0.0.1:7800/message "
            "-H 'Content-Type: application/json' "
            "-d '{\"text\":\"你好\",\"from\":\"u1\"}'\n"
            "响应: {\"reply\": \"...\", \"session\": \"bridge-u1\"}"
        )


class BridgeStatusTool(Tool):
    name = "bridge_status"
    description = "探测 webhook 桥是否在运行（默认端口 7800）"
    parameters = {"type": "object", "properties": {}}

    def run(self) -> str:
        import urllib.error
        import urllib.request
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:7800/message",
                data=b'{"text":"ping"}',
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=3) as r:
                return f"运行中（HTTP {r.status}）"
        except urllib.error.HTTPError:
            return "运行中"
        except Exception as e:  # noqa: BLE001
            return f"未在运行或不可达: {type(e).__name__}"


def setup(reg) -> None:
    reg.register(BridgeHintTool)
    reg.register(BridgeStatusTool)
