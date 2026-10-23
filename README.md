# noelle

> 终端 AI 助手 — 跨平台、可扩展、零第三方依赖

从底层重新实现，不依赖上游 Miyu 二进制。纯 Python 标准库，Windows / Linux / macOS 同源。

---

## 为什么重写

旧版 `~/noelle` 是封装上游 Miyu 二进制的方案，受限于：

- 只能跑 Linux，Windows 无构建
- 80 MB 二进制，改一行要重新编译
- 内置 logo / 标题硬编码，只能打补丁绕
- 功能不可裁剪，插件机制不透明

`noelle-ciallo` 从零重写：

| | 旧版（封装） | 新版（重写） |
|---|---|---|
| 语言 | Rust 二进制封装 | Python 3.9+ |
| 平台 | 仅 Linux | 全平台 |
| 依赖 | AUR 包 80 MB | 仅标准库 |
| 启动速度 | ~300 ms | ~80 ms |
| 可扩展 | 有限 | 工具 / 插件双层 |
| 代码可控 | 否 | 全部 |

---

## 快速开始

```bash
git clone <repo> ~/noelle-ciallo
cd ~/noelle-ciallo
./install.sh
noelle oobe          # 选模型、填 key、写人格
noelle               # 开始对话
```

不想安装，直接跑：

```bash
python3 run.py ask "你好"
```

---

## 功能

### 核心

| 功能 | 说明 |
|---|---|
| 对话 REPL | 流式输出、上下文保持、`/reset` 清空 |
| 单次提问 | `noelle ask "问题"`，支持管道 |
| 工具调用 | 模型自主决定何时调工具，结果回灌续跑 |
| 开发模式 | `noelle dev`，无人格极简形态 |
| 会话管理 | 多会话、重命名、删除、清空、移出末轮 |
| 人格 | `persona.md` 注入 system prompt |
| 记忆 | 长期事实存储 + 关键词检索 |
| 知识库 | 文档导入 + 搜索 |
| 用量统计 | token 消耗按天汇总 |

### 工具（13 个内置）

| 工具 | 作用 |
|---|---|
| `read_file` | 读文件，支持行范围 |
| `write_file` | 写/追加文件 |
| `edit_file` | 精确替换，多处匹配时拒绝 |
| `list_dir` | 列目录 |
| `find_files` | 按 glob 递归查找 |
| `grep` | 正则搜索（优先 ripgrep） |
| `run_command` | 执行 shell 命令 |
| `web_fetch` | 抓网页转纯文本 |
| `now` | 当前时间 |
| `remember_memory` | 存入长期记忆 |
| `recall_memory` | 检索记忆 |
| `search_kb` | 搜索知识库 |
| `system_info` | 系统信息 |

### 基础设施

| 模块 | 说明 |
|---|---|
| daemon | Unix socket 常驻服务，前端可复用会话 |
| shell 集成 | fish / bash / zsh 回车拦截，自然语言分流 |
| 插件系统 | Python 插件 + 脚本工具 |
| 导入导出 | 配置与人格打包，**不含 API Key** |

---

## 命令

> 上游 miyu 的 37 个子命令全部保留，仅前缀换成 `noelle`。
> 完整对照见 [`docs/命令对照.md`](docs/命令对照.md)。

```bash
noelle                       # 对话（首次引导）
noelle normal                # 对话
noelle dev                   # 开发模式
noelle ask "问题"            # 单次提问

noelle oobe                  # 新手引导
noelle init [--force]        # 初始化配置
noelle config [key] [value]  # 查看/修改配置
noelle reload                # 重载配置

noelle session list          # 会话列表
noelle session new --name x  # 新建会话
noelle session show <key>    # 查看
noelle session delete <key>  # 删除
noelle session rename <key> --name y
noelle session clear <key>   # 清空消息
noelle session pop <key> --count 2

noelle kb list               # 知识库
noelle kb add <文件>
noelle kb search --query x
noelle kb delete --key 1

noelle memory list           # 记忆
noelle memory add "内容"
noelle memory search --query x
noelle memory clear

noelle persona "你是..."     # 设置人格
noelle persona --clear       # 停用

noelle tools                 # 列出工具
noelle models                # 列出模型
noelle usage --days 7        # 用量
noelle paths                 # 所有路径

noelle daemon start|stop|restart|status
noelle plugins               # 查看已加载插件

noelle fish-init             # shell 集成
noelle bash-init
noelle zsh-init
noelle remove-shell-hook fish

noelle export [out.zip]      # 导出（无 key）
noelle import <file.zip>
noelle wipe [-y]             # 抹除全部数据
noelle reset                 # 清空终端会话上下文
noelle history [--limit 20]
```

---

## 配置

`~/.noelle/config/config.jsonc`（JSONC，支持注释）：

```jsonc
{
  "active_provider": "zhipu",
  "active_model": "glm-4-flash",
  "providers": [
    {
      "id": "zhipu",
      "base_url": "https://open.bigmodel.cn/api/paas/v4",
      "api_key": "...",
      "models": ["glm-4-flash"]
    }
  ],
  "temperature": 0.7,
  "tools": {
    "enabled": true,
    "max_rounds": 8          // 工具调用最大轮次，0 = 默认 8
  },
  "display": {
    "expand_reasoning": false,
    "show_token_usage": true
  },
  "prompt": {
    "active_persona": "persona.md"
  }
}
```

环境变量优先于配置文件：`ZHIPU_API_KEY`、`DEEPSEEK_API_KEY`、`OPENAI_API_KEY`、`OPENROUTER_API_KEY`。

`NOELLE_HOME` 可整体重定向数据目录（测试用）。

---

## 支持的模型

全部走 OpenAI 兼容协议，任何兼容端点都能接：

| provider | 默认端点 | 模型 |
|---|---|---|
| `zhipu` | open.bigmodel.cn/api/paas/v4 | glm-4-flash / glm-4-plus |
| `deepseek` | api.deepseek.com/v1 | deepseek-chat / reasoner |
| `openai` | api.openai.com/v1 | gpt-4o / gpt-4o-mini |
| `openrouter` | openrouter.ai/api/v1 | 聚合多家 |
| `ollama` | localhost:11434/v1 | 本地推理 |
| `custom` | 自填 | 任意 |

加新 provider 只需改 `noelle/llm/providers.py` 的 `PROVIDERS` 字典。

---

## 扩展

### Python 插件

放 `~/.noelle/extensions/my_tool.py`：

```python
from noelle.tools import registry, Tool

class WeatherTool(Tool):
    name = "weather"
    description = "查天气"
    parameters = {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
    }
    def run(self, city: str) -> str:
        return f"{city} 晴，25°C"

def setup(reg):
    reg.register(WeatherTool)
```

`noelle plugins` 查看是否加载。

### 脚本工具

放可执行文件到 `~/.noelle/extensions/scripts/upcase`，文件名即工具名，从 stdin 读 `{"args": "..."}`，结果打到 stdout。

---

## 目录结构

```
noelle-ciallo/
├── run.py                 入口
├── pyproject.toml
├── install.sh / uninstall.sh
├── assets/banner.txt
├── noelle/
│   ├── cli.py             命令行
│   ├── agent.py           主循环（LLM + 工具）
│   ├── config.py          JSONC 配置
│   ├── paths.py           路径解析
│   ├── daemon.py          后台服务
│   ├── llm/               客户端 + provider 表
│   ├── tools/             工具注册表 + 13 个内置
│   ├── plugins/           插件加载
│   ├── storage/           SQLite
│   ├── shell/             分类器 + hook 生成
│   └── ui/                终端输出
├── tests/                 24 项单元测试
└── docs/
```

数据目录：

```
~/.noelle/
├── config/config.jsonc
├── data/prompts/          人格
├── data/kb/               知识库
├── extensions/            插件
├── state/conversation.db  会话与记忆
└── cache/logs/
```

---

## 测试

```bash
python3 -m venv .venv
.venv/bin/pip install pytest
.venv/bin/python -m pytest tests/ -q
```

24 项测试覆盖配置、存储、工具、分类器、LLM 载荷构造，不依赖网络。

---

## 设计原则

1. **零第三方依赖** — 只用标准库，装哪都能跑
2. **单点扩展** — 加 provider 改一个字典，加工具写一个类
3. **故障隔离** — 工具异常不炸主循环，插件加载失败只警告
4. **数据可控** — SQLite 明文，随时可查可删
5. **密钥不落 git** — `export` 明确剔除 `providers`

---

## 与旧版对比

旧版 `~/noelle` 保留在仓库 `legacy` 分支思路下，功能面更全（语音、WebUI、MCP、子代理），但受限于二进制封装。新版聚焦**核心对话与工具能力**，其余按需通过插件补。

需要完整功能面时用旧版，需要可维护、跨平台、可扩展时用新版。

---

## License

MIT
