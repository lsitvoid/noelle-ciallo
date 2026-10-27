"""命令行入口：参数解析与子命令分发。"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import __version__, config as cfgmod, paths
from .agent import Agent
from .llm import LLMError, list_models
from .storage import get_db
from .tools import registry as tools
from .ui.term import C, banner, err, out, prompt_input, stream_writer, table


# ============================================================
# init
# ============================================================
def cmd_init(args) -> int:
    paths.ensure_dirs()
    f = paths.config_file()
    if f.is_file() and not args.force:
        out(f"配置已存在：{f}")
        out(f"用 --force 覆盖，或直接 noelle config 修改")
        return 0
    cfgmod.save(cfgmod.DEFAULT_CONFIG)
    # 写一份空白人格模板
    tmpl = paths.prompts_dir() / "persona.md"
    if not tmpl.is_file():
        tmpl.write_text(
            "# 人格\n\n"
            "在这里描述你希望助手是什么样的人。\n"
            "这段文字会作为 system prompt 的一部分。\n",
            encoding="utf-8",
        )
    out(f"{C.green}✓{C.reset} 配置已创建：{f}")
    out(f"{C.dim}下一步：noelle oobe 配置模型，或 noelle config 手动编辑{C.reset}")
    return 0


# ============================================================
# oobe 新手引导
# ============================================================
def cmd_oobe(args) -> int:
    paths.ensure_dirs()
    cfg = cfgmod.load()
    from .llm.providers import PROVIDERS

    out(f"{C.bold}noelle 新手引导{C.reset}")
    out("─" * 52)

    # 1. provider
    ids = [p for p in PROVIDERS if p != "custom"]
    out(f"\n{C.cyan}1. 选择模型提供方{C.reset}")
    for i, pid in enumerate(ids, 1):
        p = PROVIDERS[pid]
        out(f"   {i}) {p.name:14s} {p.base_url}")
    out(f"   {len(ids)+1}) 自定义")

    choice = prompt_input(f"{C.cyan}编号 [1] › {C.reset}").strip() or "1"
    try:
        idx = int(choice) - 1
    except ValueError:
        idx = 0
    pid = ids[idx] if 0 <= idx < len(ids) else "custom"
    prov = PROVIDERS[pid]

    base_url = prov.base_url
    if pid == "custom":
        base_url = prompt_input("base_url › ").strip()

    # 2. 模型
    out(f"\n{C.cyan}2. 模型{C.reset}")
    if prov.models:
        for i, m in enumerate(prov.models, 1):
            out(f"   {i}) {m}")
        mc = prompt_input(f"{C.cyan}编号 [1] › {C.reset}").strip() or "1"
        try:
            model = prov.models[int(mc) - 1]
        except (ValueError, IndexError):
            model = prov.models[0]
    else:
        model = prompt_input("模型名 › ").strip()

    # 3. Key
    api_key = ""
    if pid != "ollama":
        out(f"\n{C.cyan}3. API Key{C.reset}")
        if prov.api_key_env:
            out(f"   {C.dim}也可留空，改用环境变量 {prov.api_key_env}{C.reset}")
        api_key = prompt_input(f"{C.cyan}key › {C.reset}").strip()

    # 4. 人格
    out(f"\n{C.cyan}4. 人格（可留空）{C.reset}")
    persona = prompt_input(f"{C.cyan}描述 › {C.reset}").strip()

    # 保存
    entry = {
        "id": pid,
        "base_url": base_url,
        "api_key": api_key,
        "models": [model],
    }
    cfg["active_provider"] = pid
    cfg["active_model"] = model
    existing = [e for e in cfg.get("providers") or [] if e.get("id") != pid]
    cfg["providers"] = existing + [entry]

    if persona:
        pf = paths.prompts_dir() / "persona.md"
        pf.write_text(persona + "\n", encoding="utf-8")
        cfg["prompt"]["active_persona"] = "persona.md"
    else:
        cfg["prompt"]["active_persona"] = ""

    cfg["oobe_done"] = True
    cfgmod.save(cfg)

    out(f"\n{C.green}✓ 引导完成{C.reset}")
    out(f"{C.dim}配置：{paths.config_file()}{C.reset}")
    out(f"{C.dim}测试：noelle ask \"只回两个字：在线\"{C.reset}")
    return 0


# ============================================================
# ask / chat / repl
# ============================================================
def _print_event(kind: str, payload: str, state: dict) -> None:
    if kind == "text":
        if not state.get("writing"):
            sys.stdout.write(C.reset)
            state["writing"] = True
        sys.stdout.write(payload)
        sys.stdout.flush()
    elif kind == "reasoning":
        if state.get("show_reasoning"):
            sys.stdout.write(f"{C.grey}{payload}{C.reset}")
            sys.stdout.flush()
    elif kind == "tool":
        if state.get("writing"):
            sys.stdout.write("\n")
            state["writing"] = False
        out(f"{C.magenta}⚙ {payload}{C.reset}")
    elif kind == "tool_result":
        out(f"{C.dim}  ↳ {payload}{C.reset}")
    elif kind == "error":
        out(f"{C.red}✗ {payload}{C.reset}")
    elif kind == "done":
        if state.get("writing"):
            sys.stdout.write(C.reset + "\n")
            sys.stdout.flush()
            state["writing"] = False


def cmd_ask(args) -> int:
    text = " ".join(args.message).strip()
    if not text and not sys.stdin.isatty():
        text = sys.stdin.read().strip()
    if not text:
        err("用法: noelle ask \"问题\"")
        return 2

    cfg = cfgmod.load()
    a = Agent(cfg)
    sid = _auto_session(cfg, "ask")
    show_r = cfg.get("display", {}).get("expand_reasoning", False)
    state = {"show_reasoning": show_r}
    try:
        a.chat(sid, text, stream=True,
               on_event=lambda e: _print_event(e[0], e[1], state))
    except LLMError as e:
        err(str(e))
        return 1
    return 0


def cmd_repl(args, mode: str = "normal", session_name: str | None = None) -> int:
    paths.ensure_dirs()
    cfg = cfgmod.load()

    if not cfg.get("oobe_done") and not cfg.get("active_provider"):
        out(f"{C.yellow}首次使用，先跑一遍引导{C.reset}\n")
        cmd_oobe(args)
        cfg = cfgmod.load()

    db = get_db()
    # 复用终端会话
    name = session_name or f"terminal-{mode}"
    row = db.get_session(name)
    if row is None:
        sid = db.create_session(
            name, mode=mode,
            persona=cfg.get("prompt", {}).get("active_persona", ""),
            provider=cfg.get("active_provider", ""),
            model=cfg.get("active_model", ""),
        )
    else:
        sid = row["id"]

    a = Agent(cfg)
    try:
        pid, model, _ = a.resolve()
    except LLMError:
        pid, model = "未配置", ""

    if mode == "normal":
        # 独立终端 UI（miyu 式 REPL）：流式 + Markdown 重渲染 + 斜杠命令
        from .ui.tui import Tui
        return Tui(a, db, sid, name).run()

    out(f"{C.dim}{pid}/{model} · {name} · dev REPL · /exit 退出{C.reset}")
    out("")
    show_r = cfg.get("display", {}).get("expand_reasoning", False)
    while True:
        try:
            line = prompt_input(f"{C.green}你 › {C.reset}").strip()
        except SystemExit:
            break
        if not line:
            continue
        if line in ("/exit", "/quit", "exit", "quit"):
            break
        if line in ("/help", "/?"):
            out("/exit   退出\n/reset  清空当前会话上下文\n"
                "/history 最近消息\n/config 配置路径\n/tools  列出工具")
            continue
        if line == "/reset":
            db.clear_session(sid)
            out(f"{C.dim}上下文已清空{C.reset}\n")
            continue
        if line == "/config":
            out(str(paths.config_file()) + "\n")
            continue
        if line == "/tools":
            out(" ".join(tools.names()) + "\n")
            continue
        if line == "/history":
            for r in db.messages(sid)[-10:]:
                who = "你" if r["role"] == "user" else "noelle"
                out(f"{C.dim}{who}:{C.reset} {r['content'][:120]}")
            out("")
            continue

        state = {"show_reasoning": show_r}
        try:
            a.chat(sid, line, stream=True,
                   on_event=lambda e: _print_event(e[0], e[1], state))
        except LLMError as e:
            err(str(e))
        out("")
    return 0


# ============================================================
# tapall：列出全部历史对话，可选择继续
# ============================================================
def _auto_session(cfg: dict, prefix: str) -> str:
    """为一次性命令建一个落库会话（tapall 可见可恢复）"""
    from datetime import datetime
    db = get_db()
    stamp = datetime.now().strftime("%m-%d %H:%M")
    return db.create_session(
        f"{prefix}-{stamp}", mode="ask",
        persona=cfg.get("prompt", {}).get("active_persona", ""),
        provider=cfg.get("active_provider", ""),
        model=cfg.get("active_model", ""),
    )


def cmd_tapall(args) -> int:
    db = get_db()
    rows = db.list_sessions(limit=200)
    if not rows:
        out("(还没有任何对话)")
        return 0

    def _preview(row) -> str:
        ms = db.messages(row["id"], limit=1)
        return (ms[-1]["content"].replace("\n", " ")[:48]) if ms else "（空会话）"

    def _ago(ts: float) -> str:
        d = time.time() - ts
        if d < 3600:
            return f"{int(d // 60)} 分钟前"
        if d < 86400:
            return f"{int(d // 3600)} 小时前"
        return f"{int(d // 86400)} 天前"

    # 指定了序号 → 直接恢复该会话进 REPL
    if args.index:
        try:
            row = rows[int(args.index) - 1]
        except (ValueError, IndexError):
            err(f"序号超出范围: {args.index}（1-{len(rows)}）")
            return 1
        out(f"{C.dim}继续 {C.reset}{row['name']}"
            f" {C.dim}· {row['msgs']} 条消息 · /exit 退出{C.reset}\n")
        return cmd_repl(args, mode=row["mode"], session_name=row["name"])

    # 无参数 → 列表
    body = [
        [str(i + 1), r["name"], r["mode"], str(r["msgs"]),
         _ago(r["updated_at"]), _preview(r)]
        for i, r in enumerate(rows)
    ]
    out(table(body, ["#", "会话", "模式", "消息", "最近", "预览"]))
    out(f"{C.dim}继续某次对话: noelle tapall <序号>{C.reset}")
    return 0


# ============================================================
# session
# ============================================================
def cmd_session(args) -> int:
    db = get_db()
    sub = args.action
    if sub == "list" or sub is None:
        rows = db.list_sessions()
        if not rows:
            out("(无会话)")
            return 0
        data = []
        for i, r in enumerate(rows, 1):
            data.append([
                str(i), r["name"], r["mode"],
                str(r["msgs"]),
                time.strftime("%m-%d %H:%M", time.localtime(r["updated_at"])),
            ])
        out(table(data, ["#", "名称", "模式", "消息", "更新"]))
        return 0

    if sub == "new":
        sid = db.create_session(
            args.name or f"session-{int(time.time())}",
            mode=args.mode or "normal",
        )
        out(f"{C.green}✓{C.reset} 已创建会话 {sid}")
        return 0

    row = db.get_session(args.key or "")
    if not row:
        err(f"会话不存在: {args.key}")
        return 1
    sid = row["id"]

    if sub == "show":
        out(f"id      : {sid}")
        out(f"名称    : {row['name']}")
        out(f"模式    : {row['mode']}")
        out(f"人格    : {row['persona'] or '(无)'}")
        out(f"模型    : {row['provider']}/{row['model']}")
        out(f"消息数  : {db.message_count(sid)}")
        return 0
    if sub == "delete":
        db.delete_session(sid)
        out(f"{C.green}✓{C.reset} 已删除")
        return 0
    if sub == "rename":
        db.rename_session(sid, args.name or "")
        out(f"{C.green}✓{C.reset} 已重命名")
        return 0
    if sub == "clear":
        db.clear_session(sid)
        out(f"{C.green}✓{C.reset} 已清空")
        return 0
    if sub == "pop":
        n = db.pop_messages(sid, args.count or 1)
        out(f"{C.green}✓{C.reset} 移出 {n} 条消息")
        return 0
    err(f"未知操作: {sub}")
    return 1


# ============================================================
# kb / memory
# ============================================================
def cmd_kb(args) -> int:
    db = get_db()
    sub = args.action
    if sub == "list" or sub is None:
        rows = db.list_kb()
        if not rows:
            out("(知识库为空)")
            return 0
        data = [[str(r["id"]), r["title"], r["path"]] for r in rows]
        out(table(data, ["id", "标题", "路径"]))
        return 0
    if sub == "add":
        p = Path(args.path).expanduser()
        if not p.is_file():
            err(f"文件不存在: {p}")
            return 1
        content = p.read_text(encoding="utf-8", errors="replace")
        doc_id = db.add_kb_doc(p.stem, str(p), content)
        out(f"{C.green}✓{C.reset} 已导入 id={doc_id}（{len(content)} 字符）")
        return 0
    if sub == "search":
        rows = db.search_kb(args.query or "")
        for r in rows:
            out(f"{C.bold}## {r['title']}{C.reset}")
            out(r["content"][:300])
            out("")
        return 0
    if sub == "delete":
        db.delete_kb_doc(int(args.key or 0))
        out(f"{C.green}✓{C.reset} 已删除")
        return 0
    err(f"未知操作: {sub}")
    return 1


def cmd_memory(args) -> int:
    db = get_db()
    sub = args.action
    if sub == "list" or sub is None:
        kind = getattr(args, "kind", None)
        rows = (db.memories_by_kind(kind, limit=200) if kind
                else db.list_memories(limit=200))
        if not rows:
            out("(无记忆)")
            return 0
        for r in rows:
            keys = r.keys()
            k = r["kind"] if "kind" in keys else "plain"
            tag = {"user": C.cyan + "用户" + C.reset,
                   "self": C.green + "自己" + C.reset,
                   "event": C.yellow + "事件" + C.reset}.get(
                k, C.dim + "通用" + C.reset)
            out(f"{C.dim}[{r['id']}]{C.reset} {tag} {r['content']}")
        return 0
    if sub == "add":
        mid = db.add_memory(" ".join(args.text or []))
        out(f"{C.green}✓{C.reset} 已记住 id={mid}")
        return 0
    if sub == "search":
        rows = db.search_memories(args.query or "")
        for r in rows:
            out(f"{C.dim}[{r['id']}]{C.reset} {r['content']}")
        return 0
    if sub == "delete":
        db.delete_memory(int(args.key or 0))
        out(f"{C.green}✓{C.reset} 已删除")
        return 0
    if sub == "clear":
        n = db.clear_memories()
        out(f"{C.green}✓{C.reset} 已清空 {n} 条")
        return 0
    err(f"未知操作: {sub}")
    return 1


# ============================================================
# 杂项
# ============================================================
def cmd_paths(args) -> int:
    for k, v in paths.all_paths().items():
        out(f"{k:12s} {v}")
    return 0


def cmd_mood(args) -> int:
    """心情状态与时间线（大肥鱼式情感系统）"""
    from . import mood
    db = get_db()
    if args.action == "reset":
        mood.reset(db)
        out(f"{C.green}✓{C.reset} 已重置到基线 {mood.BASELINE}")
        return 0
    if args.action == "off":
        cfg = cfgmod.load()
        cfg.setdefault("mood", {})["enabled"] = False
        cfgmod.save(cfg)
        out("心情判定已关闭")
        return 0
    if args.action == "on":
        cfg = cfgmod.load()
        cfg.setdefault("mood", {})["enabled"] = True
        cfgmod.save(cfg)
        out("心情判定已开启")
        return 0

    st = mood.current_state(db)
    bar_n = st["mood"] // 5
    out(f"心情 {C.cyan}{st['mood']}/100{C.reset} "
        f"{C.dim}{'█' * bar_n}{'░' * (20 - bar_n)}{C.reset} "
        f"{mood.face(st['mood'])}")
    emo = "  ".join(f"{k}{v:+d}" for k, v in st["emotions"].items())
    out(f"七情: {emo}")
    tl = mood.timeline(db)
    if tl:
        out(f"\n{C.dim}时间线:{C.reset}")
        for r in tl:
            d = f"{r['delta']:+d}"
            col = C.green if r["delta"] > 0 else (C.red if r["delta"] < 0 else C.dim)
            t = time.strftime("%m-%d %H:%M", time.localtime(r["ts"]))
            out(f"  {C.dim}{t}{C.reset} {col}{d:>3}{C.reset} → {r['mood']:3d}  {r['reason']}")
    else:
        out(f"{C.dim}还没有心情记录，聊两句就有了{C.reset}")
    return 0


def cmd_models(args) -> int:
    cfg = cfgmod.load()
    cur_p = cfg.get("active_provider")
    cur_m = cfg.get("active_model")
    rows = []
    for pid, model, name in list_models(cfg):
        mark = "→" if (pid == cur_p and model == cur_m) else " "
        rows.append([mark, pid, model, name])
    if rows:
        out(table(rows, ["", "provider", "model", "名称"]))
    else:
        out("(未配置模型)")
    return 0


def cmd_tools(args) -> int:
    rows = []
    for t in tools.all():
        rows.append([t.name, "⚠" if t.dangerous else "", t.description[:50]])
    out(table(rows, ["名称", "", "说明"]))
    return 0


def cmd_config(args) -> int:
    cfg = cfgmod.load()
    if args.key and args.value is not None:
        try:
            val = json.loads(args.value)
        except json.JSONDecodeError:
            val = args.value
        cfgmod.set_value(args.key, val)
        out(f"{C.green}✓{C.reset} {args.key} = {val}")
        return 0
    if args.key:
        out(json.dumps(cfgmod.get(args.key), ensure_ascii=False, indent=2))
        return 0
    out(json.dumps(cfg, ensure_ascii=False, indent=2))
    return 0


def cmd_history(args) -> int:
    db = get_db()
    row = db.get_session(args.session or "terminal-normal")
    if not row:
        out("(无会话)")
        return 0
    for r in db.messages(row["id"])[-int(args.limit or 20):]:
        who = "你" if r["role"] == "user" else "noelle"
        ts = time.strftime("%H:%M", time.localtime(r["created_at"]))
        out(f"{C.dim}{ts} {who}:{C.reset} {r['content'][:200]}")
    return 0


def cmd_reset(args) -> int:
    db = get_db()
    row = db.get_session("terminal-normal")
    if row:
        db.clear_session(row["id"])
        out(f"{C.green}✓{C.reset} 终端会话上下文已清空")
    return 0


def cmd_wipe(args) -> int:
    if not args.yes:
        ans = prompt_input("将删除所有会话、消息、记忆、用量记录。确认？[y/N] ")
        if ans.strip().lower() not in ("y", "yes"):
            out("已取消")
            return 0
    db = get_db()
    db.wipe()
    db.vacuum()
    out(f"{C.green}✓{C.reset} 已抹除")
    return 0


def cmd_persona(args) -> int:
    cfg = cfgmod.load()
    pf = paths.prompts_dir() / "persona.md"
    if args.text:
        paths.ensure_dirs()
        pf.write_text(" ".join(args.text) + "\n", encoding="utf-8")
        cfg["prompt"]["active_persona"] = "persona.md"
        cfgmod.save(cfg)
        out(f"{C.green}✓{C.reset} 人格已保存")
        return 0
    if args.clear:
        cfg["prompt"]["active_persona"] = ""
        cfgmod.save(cfg)
        out(f"{C.green}✓{C.reset} 人格已停用")
        return 0
    if pf.is_file():
        out(pf.read_text(encoding="utf-8"))
    else:
        out("(未设置人格)")
    return 0


def cmd_export(args) -> int:
    import zipfile
    cfg = cfgmod.load()
    cfg.pop("providers", None)  # 不导出 key
    out_path = Path(args.output or "noelle-export.zip").expanduser()
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("config.json", json.dumps(cfg, ensure_ascii=False, indent=2))
        for f in paths.prompts_dir().glob("*.md"):
            z.write(f, f"prompts/{f.name}")
    out(f"{C.green}✓{C.reset} 已导出 {out_path}（不含 API Key）")
    return 0


def cmd_import(args) -> int:
    import zipfile
    src = Path(args.archive).expanduser()
    if not src.is_file():
        err(f"归档不存在: {src}")
        return 1
    paths.ensure_dirs()
    with zipfile.ZipFile(src) as z:
        for name in z.namelist():
            if name == "config.json":
                data = json.loads(z.read(name))
                cur = cfgmod.load()
                cur.update({k: v for k, v in data.items() if k != "providers"})
                cfgmod.save(cur)
            elif name.startswith("prompts/") and name.endswith(".md"):
                (paths.prompts_dir() / Path(name).name).write_bytes(z.read(name))
    out(f"{C.green}✓{C.reset} 已导入")
    return 0


def cmd_reload(args) -> int:
    out(f"{C.green}✓{C.reset} 配置已重新加载")
    return 0


# ============================================================
# shell 集成
# ============================================================
def cmd_shell_init(args) -> int:
    from .shell import generate
    from .shell.hooks import hooks_path
    text = generate(args.shell)
    if not text:
        err(f"不支持的 shell: {args.shell}")
        return 1
    target = Path(hooks_path(args.shell)).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        bak = target.with_suffix(target.suffix + ".bak")
        bak.write_bytes(target.read_bytes())
    target.write_text(text, encoding="utf-8")
    out(f"{C.green}✓{C.reset} 已写入 {target}")
    if args.shell == "fish":
        out(f"{C.dim}新开终端或执行: source {target}{C.reset}")
    else:
        out(f"{C.dim}重开终端或执行: source {target}{C.reset}")
    return 0


def cmd_shell_remove(args) -> int:
    from .shell.hooks import hooks_path
    target = Path(hooks_path(args.shell)).expanduser()
    if target.is_file() and "noelle" in target.read_text(encoding="utf-8", errors="ignore"):
        target.unlink()
        out(f"{C.green}✓{C.reset} 已移除 {target}")
    else:
        out("(未找到 noelle hook)")
    return 0


def cmd_shell_classify(args) -> int:
    from .shell import classify
    line = sys.stdin.read() if args.stdin else " ".join(args.text or [])
    print(classify(line))
    # 退出码对齐 miyu shell-hook 约定：1=自然语言（hook 接管），0=放行
    return 1 if classify(line) == "natural" else 0


def cmd_shell_intercept(args) -> int:
    """被 shell hook 调用：把自然语言交给 noelle，打印结果"""
    from .shell import classify
    line = " ".join(args.text or []).strip()
    if not line:
        line = sys.stdin.read().strip()
    if not line:
        return 0
    if classify(line) != "natural":
        # 不是自然语言就交回 shell
        print(line)
        return 0
    args.message = [line]
    return cmd_ask(args)


# ============================================================
# daemon
# ============================================================
def cmd_daemon(args) -> int:
    from . import daemon
    sub = args.action or "status"

    if sub == "start":
        st = daemon.status()
        if st.get("running"):
            out(f"已在运行 pid={st['pid']}")
            return 0
        import subprocess
        env = dict(os.environ)
        subprocess.Popen(
            [sys.executable, "-m", "noelle.daemon"],
            env=env, start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for _ in range(50):
            time.sleep(0.1)
            if daemon.status().get("running"):
                break
        st = daemon.status()
        if st.get("running"):
            out(f"{C.green}✓{C.reset} daemon 已启动 pid={st['pid']}")
            return 0
        err("启动失败（查看 ~/.noelle/cache/logs）")
        return 1

    if sub == "stop":
        if daemon.stop():
            out(f"{C.green}✓{C.reset} daemon 已停止")
        else:
            out("(未运行)")
        return 0

    if sub == "restart":
        daemon.stop()
        time.sleep(0.3)
        args.action = "start"
        return cmd_daemon(args)

    st = daemon.status()
    if st.get("running"):
        out(f"{C.green}运行中{C.reset} pid={st['pid']}")
        out(f"socket: {st['socket']}")
    else:
        out("未运行")
    return 0


# ============================================================
# 插件
# ============================================================
def cmd_plugins(args) -> int:
    from .plugins import discover, set_enabled
    sub = args.action or "list"

    if sub == "list":
        items = discover()
        if not items:
            out("(无插件)")
            return 0
        rows = [
            ["✓" if i["enabled"] else "✗", i["name"], i["source"]]
            for i in items
        ]
        out(table(rows, ["", "插件", "来源"]))
        out(f"{C.dim}启用/停用: noelle plugins enable|disable <名>{C.reset}")
        return 0

    if sub in ("enable", "disable"):
        name = args.name or ""
        if set_enabled(name, sub == "enable"):
            mark = f"{C.green}✓ 已启用{C.reset}" if sub == "enable" else f"{C.yellow}已停用{C.reset}"
            out(f"{mark}: {name}")
            return 0
        err(f"插件不存在: {name}")
        return 1

    err(f"未知操作: {sub}")
    return 1


# ============================================================
# 语音（占位，需可选依赖）
# ============================================================
def cmd_stt(args) -> int:
    err("语音功能需要可选依赖: pip install noelle[voice]")
    return 1


def cmd_voice(args) -> int:
    err("语音功能需要可选依赖: pip install noelle[voice]")
    return 1


# ============================================================
# 用量
# ============================================================
def cmd_usage(args) -> int:
    db = get_db()
    since = 0.0
    if args.days:
        since = time.time() - int(args.days) * 86400
    u = db.usage_summary(since)
    out(f"调用次数: {u['calls']}")
    out(f"输入 token: {u['prompt']}")
    out(f"输出 token: {u['completion']}")
    out(f"合计      : {u['prompt'] + u['completion']}")
    return 0


# ============================================================
# 完整命令集
# ============================================================
def cmd_pop(args) -> int:
    db = get_db()
    row = db.get_session(args.session or "terminal-normal")
    if not row:
        err("无终端会话")
        return 1
    n = db.pop_messages(row["id"], args.count or 1)
    out(f"{C.green}✓{C.reset} 移出 {n} 条")
    return 0


def cmd_compact(args) -> int:
    """压缩上下文：保留最近 N 条，其余丢弃。"""
    db = get_db()
    row = db.get_session(args.session or "terminal-normal")
    if not row:
        err("无终端会话")
        return 1
    sid = row["id"]
    total = db.message_count(sid)
    keep = args.keep or 10
    if total <= keep:
        out(f"消息 {total} 条，无需压缩")
        return 0
    drop = total - keep
    db.pop_messages(sid, drop)
    out(f"{C.green}✓{C.reset} 已压缩：{total} → {keep} 条")
    return 0


def cmd_reset_memory(args) -> int:
    db = get_db()
    row = db.get_session(args.session or "terminal-normal")
    n = db.clear_memories(row["persona"] if row else "")
    out(f"{C.green}✓{C.reset} 清空 {n} 条记忆")
    return 0


def cmd_reset_all_memory(args) -> int:
    cfg = cfgmod.load()
    persona = cfg.get("prompt", {}).get("active_persona", "")
    n = get_db().clear_memories(persona)
    out(f"{C.green}✓{C.reset} 人格 {persona or '(无)'} 的记忆已清空（{n} 条）")
    return 0


def cmd_skills(args) -> int:
    d = paths.extensions_dir() / "skills"
    d.mkdir(parents=True, exist_ok=True)
    sub = args.action or "list"
    if sub == "list":
        items = sorted(d.iterdir()) if d.is_dir() else []
        if not items:
            out(f"(skills 为空)\n{C.dim}目录: {d}{C.reset}")
            return 0
        for f in items:
            out(f"· {f.name}")
        return 0
    if sub == "dir":
        out(str(d))
        return 0
    if sub == "run":
        target = d / (args.name or "")
        if not (target / "SKILL.md").is_file() and target.suffix != ".md":
            err(f"skill 不存在: {target}")
            return 1
        md = target / "SKILL.md" if target.is_dir() else target
        args.message = [f"请按以下 skill 执行：\n\n{md.read_text(encoding='utf-8')}"]
        return cmd_ask(args)
    err(f"未知操作: {sub}")
    return 1


def cmd_stdio(args) -> int:
    """长驻协议：stdin 一行一请求（JSON），stdout 一行一事件。"""
    from .agent import Agent
    agent = Agent()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            print(json.dumps({"event": "error", "message": "JSON 解析失败"},
                             ensure_ascii=False), flush=True)
            continue
        op = req.get("op", "ask")
        if op == "exit":
            break
        if op != "ask":
            print(json.dumps({"event": "error", "message": f"未知 op: {op}"},
                             ensure_ascii=False), flush=True)
            continue
        try:
            turn = agent.run(
                req.get("text", ""), stream=False,
            )
            print(json.dumps({
                "event": "done", "text": turn.text,
                "tokens": [turn.prompt_tokens, turn.completion_tokens],
            }, ensure_ascii=False), flush=True)
        except LLMError as e:
            print(json.dumps({"event": "error", "message": str(e)},
                             ensure_ascii=False), flush=True)
    return 0


def cmd_tool_call(args) -> int:
    """以本会话身份直接调用一个工具，绕过 LLM。"""
    from .plugins import load_all
    load_all(cfgmod.load())
    name = args.name or ""
    try:
        call_args = json.loads(args.args) if args.args else {}
    except json.JSONDecodeError:
        err("--args 需是合法 JSON")
        return 2
    res = tools.call(name, call_args)
    if res.ok:
        out(res.content)
        return 0
    err(res.error)
    return 1


def cmd_web(args) -> int:
    """本地 WebUI：LobeChat 风格，SSE 流式 + Markdown + 会话侧栏。"""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from .agent import Agent

    host = args.host or "127.0.0.1"
    port = args.port or 7789
    page = r"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>noelle</title>
<style>
:root{--bg:#f9fafb;--panel:#ffffff;--line:#e5e7eb;--fg:#111827;--dim:#6b7280;
      --acc:#6366f1;--acc2:#8b5cf6;--user-bg:linear-gradient(135deg,#6366f1,#8b5cf6);
      --bot-bg:#f3f4f6;--code:#1f2937;
      --dark-bg:#111827;--dark-panel:#1f2937}
@media (prefers-color-scheme: dark){
  :root{--bg:#111827;--panel:#1f2937;--line:#374151;--fg:#f3f4f6;--dim:#9ca3af;
        --bot-bg:#1f2937;--code:#0b1020}}
*{margin:0;padding:0;box-sizing:border-box}
html,body{height:100%}
body{font:15px/1.7 -apple-system,'Segoe UI','Noto Sans SC',system-ui,sans-serif;
     background:var(--bg);color:var(--fg);display:flex;overflow:hidden}
/* ── 侧栏 ── */
#side{width:248px;flex:none;background:var(--panel);border-right:1px solid var(--line);
      display:flex;flex-direction:column;transition:margin .25s}
#side.hide{margin-left:-248px}
#side h1{font-size:16px;padding:18px 18px 12px;letter-spacing:.04em;
     background:linear-gradient(90deg,var(--acc),var(--acc2));
     -webkit-background-clip:text;background-clip:text;color:transparent}
#newchat{margin:0 14px 10px;padding:9px;border:1px dashed var(--acc);border-radius:10px;
     background:none;color:var(--acc);cursor:pointer;font:inherit;font-size:13.5px;
     transition:background .15s}
#newchat:hover{background:rgba(99,102,241,.08)}
#sess{flex:1;overflow-y:auto;padding:4px 10px;scrollbar-width:thin}
.sess-item{padding:9px 12px;border-radius:9px;cursor:pointer;font-size:13.5px;
     color:var(--dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
     margin-bottom:2px;transition:background .15s}
.sess-item:hover{background:rgba(99,102,241,.08)}
.sess-item.cur{background:rgba(99,102,241,.13);color:var(--acc);font-weight:600}
#sfoot{padding:12px 16px;border-top:1px solid var(--line);font-size:11.5px;
     color:var(--dim);flex:none}
/* ── 主区 ── */
#main{flex:1;display:flex;flex-direction:column;min-width:0}
#top{flex:none;display:flex;align-items:center;gap:10px;padding:12px 20px;
     border-bottom:1px solid var(--line);background:var(--panel)}
#burger{background:none;border:none;font-size:18px;color:var(--dim);cursor:pointer}
#model-badge{font-size:12px;color:var(--acc);border:1px solid var(--acc);
     border-radius:20px;padding:2px 12px}
#log{flex:1;overflow-y:auto;padding:26px 24px;display:flex;flex-direction:column;
     gap:18px;scrollbar-width:thin}
.msg{display:flex;gap:12px;max-width:820px;width:100%;margin:0 auto;
     animation:fadein .25s ease}
@keyframes fadein{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
.ava{width:34px;height:34px;border-radius:10px;flex:none;display:flex;
     align-items:center;justify-content:center;font-size:15px;color:#fff;
     background:var(--user-bg)}
.msg.bot .ava{background:linear-gradient(135deg,#10b981,#0ea5e9)}
.bubble{min-width:0;padding:12px 16px;border-radius:14px;line-height:1.75}
.msg.user .bubble{background:var(--user-bg);color:#fff;border-top-left-radius:4px}
.msg.bot .bubble{background:var(--bot-bg);border:1px solid var(--line);
     border-top-right-radius:4px;flex:1}
.bubble p{margin:.45em 0}.bubble p:first-child{margin-top:0}
.bubble pre{background:var(--code);color:#e5e7eb;padding:12px 14px;border-radius:10px;
     overflow-x:auto;margin:.7em 0;font-size:13px}
.bubble code{font-family:ui-monospace,Consolas,monospace;font-size:.9em;
     background:rgba(99,102,241,.1);padding:.12em .4em;border-radius:5px}
.bubble pre code{background:none;padding:0}
.bubble h1,.bubble h2,.bubble h3{margin:.7em 0 .35em}
.bubble ul,.bubble ol{margin:.4em 0 .4em 1.4em}
.bubble a{color:var(--acc)}
.bubble blockquote{border-left:3px solid var(--acc);padding-left:12px;
     color:var(--dim);margin:.5em 0}
.bubble table{border-collapse:collapse;margin:.6em 0}
.bubble th,.bubble td{border:1px solid var(--line);padding:5px 12px}
.reasoning{font-size:12.5px;color:var(--dim);background:rgba(99,102,241,.05);
     border-left:2px solid var(--acc);padding:6px 10px;margin-bottom:8px;
     border-radius:0 8px 8px 0;white-space:pre-wrap;cursor:pointer}
.cursor::after{content:"●";color:var(--acc);font-size:10px;
     animation:bl 1s steps(2) infinite;margin-left:4px}
@keyframes bl{50%{opacity:0}}
/* ── 输入区 ── */
footer{flex:none;padding:14px 24px 18px;background:var(--panel);
     border-top:1px solid var(--line)}
#row{display:flex;gap:10px;max-width:820px;margin:0 auto;align-items:flex-end}
textarea{flex:1;resize:none;background:var(--bg);border:1px solid var(--line);
     border-radius:14px;color:var(--fg);font:inherit;padding:12px 16px;outline:none;
     min-height:48px;max-height:180px;transition:border-color .2s,box-shadow .2s}
textarea:focus{border-color:var(--acc);box-shadow:0 0 0 3px rgba(99,102,241,.12)}
#send{background:var(--user-bg);color:#fff;border:none;border-radius:14px;
     padding:0 24px;font:inherit;font-weight:600;cursor:pointer;height:48px;
     transition:opacity .2s,transform .15s}
#send:hover{transform:translateY(-1px)}
#send:disabled{opacity:.4;cursor:default;transform:none}
.tip{text-align:center;color:var(--dim);font-size:11.5px;margin-top:8px}
@media (max-width:768px){#side{position:fixed;z-index:50;height:100%}
     .msg{max-width:100%}#log{padding:18px 12px}}
</style>
<header id="top" style="display:flex">
<button id="burger">☰</button>
<b style="font-size:15px">noelle</b>
<span id="model-badge">…</span></header>
<div id="side"><h1>noelle</h1>
<button id="newchat">＋ 新会话</button>
<div id="sess"></div>
<div id="sfoot">noelle-ciallo · 本地运行</div></div>
<div id="log"></div>
<footer><div id="row">
<textarea id="q" rows="1" placeholder="给 noelle 发消息…（Enter 发送）"></textarea>
<button id="send">发送</button></div>
<div class="tip">Enter 发送 · Shift+Enter 换行</div></footer>
<script src="https://cdn.jsdelivr.net/npm/marked@12/marked.min.js"></script>
<script>
const log=document.getElementById('log'),q=document.getElementById('q'),
      send=document.getElementById('send'),sessEl=document.getElementById('sess'),
      badge=document.getElementById('model-badge');
let sid=localStorage.getItem('sid')||'',busy=false;
function add(cls,html){
  const d=document.createElement('div');d.className='msg '+cls;
  d.innerHTML='<div class="ava">'+(cls==='user'?'你':'N')+'</div>'+
              '<div class="bubble">'+(html||'')+'</div>';
  log.appendChild(d);log.scrollTop=log.scrollHeight;return d.lastElementChild;}
function bottom(){log.scrollTop=log.scrollHeight;}
function render(el,md){el.innerHTML=marked.parse(md);bottom();}
async function loadSess(){
  try{const j=await(await fetch('/sessions')).json();
    sessEl.innerHTML='';
    (j.sessions||[]).forEach(s=>{
      const d=document.createElement('div');
      d.className='sess-item'+(s.id===sid?' cur':'');
      d.textContent=s.name||s.id.slice(0,8);
      d.title=s.name||s.id;
      d.onclick=()=>{sid=s.id;localStorage.setItem('sid',sid);switchTo(s);};
      sessEl.appendChild(d);});
  }catch(e){}}
async function switchTo(s){
  log.innerHTML='';
  try{const j=await(await fetch('/history?session='+encodeURIComponent(s.id))).json();
    (j.messages||[]).forEach(m=>{
      if(m.role==='user')add('user','<div class="bubble-in"></div>');
      const el=add(m.role==='user'?'user':'bot');
      render(el, m.content||'');});}catch(e){}
  loadSess();}
document.getElementById('newchat').onclick=()=>{
  sid='';localStorage.removeItem('sid');log.innerHTML='';
  add('bot','新会话已就绪，说点什么吧～');loadSess();};
document.getElementById('burger').onclick=()=>{
  document.getElementById('side').classList.toggle('hide');};
q.addEventListener('input',()=>{q.style.height='auto';
  q.style.height=Math.min(q.scrollHeight,180)+'px';});
async function ask(){
  const text=q.value.trim();
  if(!text||busy)return;
  busy=true;send.disabled=true;q.value='';q.style.height='auto';
  add('user').innerHTML='<div class="bubble"></div>';
  log.lastElementChild.querySelector('.bubble').textContent=text;
  const b=add('bot');
  const think=document.createElement('div');think.className='reasoning';
  think.style.display='none';b.appendChild(think);
  const body=document.createElement('div');body.className='cursor';
  b.appendChild(body);
  let acc='',racc='';
  try{
    const r=await fetch('/ask',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({text,session:sid})});
    const rd=r.body.getReader(),dec=new TextDecoder();
    let buf='';
    for(;;){
      const {done,value}=await rd.read();if(done)break;
      buf+=dec.decode(value,{stream:true});
      const parts=buf.split('\n\n');buf=parts.pop();
      for(const p of parts){
        if(!p.startsWith('data: '))continue;
        let ev;try{ev=JSON.parse(p.slice(6));}catch(e){continue;}
        if(ev.t==='text'){acc+=ev.d;render(body,acc);}
        else if(ev.t==='reasoning'){racc+=ev.d;think.style.display='block';
          think.textContent=racc;bottom();}
        else if(ev.t==='error'){body.textContent='⚠ '+ev.d;}
        else if(ev.t==='end'){
          sid=ev.session;localStorage.setItem('sid',sid);
          body.classList.remove('cursor');
          if(racc){think.textContent='💭 思考过程（点击折叠/展开）';
            think.onclick=()=>{const open=think.textContent.includes('💭');
              think.textContent=open?racc:'💭 思考过程（点击折叠/展开）';};}
          render(body,ev.text||acc);loadSess();
        }
      }
    }
  }catch(e){body.textContent='⚠ 连接失败：'+e;}
  body.classList.remove('cursor');
  busy=false;send.disabled=false;q.focus();}
send.onclick=ask;
q.addEventListener('keydown',e=>{
  if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();ask();}});
fetch('/meta').then(r=>r.json()).then(j=>{badge.textContent=j.desc||'noelle';});
loadSess();
if(!sid)add('bot','你好，我是 noelle。说什么都可以～');
q.focus();
</script></html>"""

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, ctype, body):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", page.encode())
            elif self.path == "/meta":
                cfg = Agent().cfg
                d = cfg.get("active_provider", "") + "/" + cfg.get("active_model", "")
                self._send(200, "application/json",
                           json.dumps({"desc": d}).encode())
            elif self.path.startswith("/sessions"):
                db = get_db()
                rows = db.list_sessions(limit=60)
                data = [{"id": r["id"], "name": r["name"]} for r in rows]
                self._send(200, "application/json",
                           json.dumps({"sessions": data}, ensure_ascii=False).encode())
            elif self.path.startswith("/history"):
                import urllib.parse as _u
                qs = _u.parse_qs(_u.urlparse(self.path).query)
                sid = (qs.get("session") or [""])[0]
                db = get_db()
                msgs = db.messages(sid) if sid and db.get_session(sid) else []
                data = [{"role": m["role"], "content": m["content"]} for m in msgs]
                self._send(200, "application/json",
                           json.dumps({"messages": data}, ensure_ascii=False).encode())
            else:
                self.send_error(404)

        def do_POST(self):
            if self.path != "/ask":
                self.send_error(404)
                return
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
            db = get_db()
            sid = req.get("session") or ""
            if not sid or not db.get_session(sid):
                sid = db.create_session(f"web-{int(time.time())}")

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()

            def sse(obj):
                self.wfile.write(
                    f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode())
                self.wfile.flush()

            agent = Agent()
            try:
                turn = agent.chat(
                    sid, req.get("text", ""), stream=True,
                    on_event=lambda ev: sse({"t": ev[0], "d": ev[1]})
                    if ev[0] in ("text", "reasoning", "error") else None,
                )
                cfg = agent.cfg
                meta = cfg.get("active_provider", "") + "/" + cfg.get("active_model", "")
                sse({"t": "end", "text": turn.text, "session": sid, "meta": meta})
            except LLMError as e:
                sse({"t": "end", "text": f"⚠ {e}", "session": sid})
            except (BrokenPipeError, ConnectionResetError):
                pass

    srv = ThreadingHTTPServer((host, port), H)
    out(f"{C.green}✓{C.reset} WebUI: http://{host}:{port}")
    out(f"{C.dim}Ctrl+C 停止{C.reset}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        out("\n已停止")
    finally:
        srv.server_close()
    return 0


def cmd_bridge(args) -> int:
    """webhook 桥：前台常驻，供外部聊天软件接入。"""
    from .bridge import serve
    serve(args.port or 7800)
    return 0


def cmd_layout(args) -> int:
    out(f"{C.bold}数据布局{C.reset}")
    for k, v in paths.all_paths().items():
        p = Path(v)
        mark = "✓" if p.exists() else " "
        out(f"  {mark} {k:12s} {v}")
    if args.apply:
        paths.ensure_dirs()
        out(f"{C.green}✓{C.reset} 目录已创建")
    return 0


def cmd_embed(args) -> int:
    db = get_db()
    sub = args.action or "status"
    docs = db.list_kb()
    if sub == "status":
        out(f"知识库条目: {len(docs)}")
        out(f"{C.dim}embedding 需可选依赖，当前使用关键词检索{C.reset}")
        return 0
    if sub == "rebuild":
        # 关键词索引无需重建，仅提示
        out(f"{C.green}✓{C.reset} 关键词索引就绪（{len(docs)} 条）")
        return 0
    if sub == "models":
        out("(未安装 embedding 模型)")
        return 0
    err(f"未知操作: {sub}")
    return 1


def cmd_effort(args) -> int:
    cfg = cfgmod.load()
    if args.level:
        cfg["effort"] = args.level
        cfgmod.save(cfg)
        out(f"{C.green}✓{C.reset} 思考档位 = {args.level}")
        return 0
    out(f"当前档位: {cfg.get('effort', 'default')}")
    return 0


def cmd_listen(args) -> int:
    err("语音收听需可选依赖: pip install noelle[voice]")
    return 1


def cmd_update_default_kb(args) -> int:
    import zipfile
    from . import __file__ as pkg_file
    src = Path(pkg_file).parent.parent / "assets" / "default-kb.zip"
    if not src.is_file():
        out("(未内置默认知识库，跳过)")
        return 0
    db = get_db()
    with zipfile.ZipFile(src) as z:
        for n in z.namelist():
            if n.endswith(".md"):
                db.add_kb_doc(Path(n).stem, n, z.read(n).decode("utf-8", "replace"))
    out(f"{C.green}✓{C.reset} 默认知识库已更新")
    return 0


def cmd_list_models(args) -> int:
    return cmd_models(args)


# ============================================================
# 参数解析
# ============================================================
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="noelle",
        description="noelle — 终端 AI 助手",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  noelle                      进入对话\n"
            "  noelle ask \"解释 Btrfs 快照\"\n"
            "  noelle kb add README.md\n"
            "  noelle session list\n"
        ),
    )
    p.add_argument("-v", "--version", action="version", version=f"noelle {__version__}")
    p.add_argument("--stdout", action="store_true", help="纯文本输出")
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("init", help="创建配置文件")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("oobe", help="新手引导")
    sp.set_defaults(func=cmd_oobe)

    sp = sub.add_parser("normal", help="普通对话模式")
    sp.set_defaults(func=lambda a: cmd_repl(a, "normal"))

    sp = sub.add_parser("dev", help="开发模式（无人格）")
    sp.set_defaults(func=lambda a: cmd_repl(a, "dev"))

    sp = sub.add_parser("ask", help="单次提问")
    sp.add_argument("message", nargs="*")
    sp.set_defaults(func=cmd_ask)

    sp = sub.add_parser("session", help="会话管理")
    sp.add_argument("action", nargs="?", default="list",
                    choices=["list", "new", "show", "delete", "rename", "clear", "pop"])
    sp.add_argument("key", nargs="?", default="")
    sp.add_argument("--name")
    sp.add_argument("--mode", choices=["normal", "dev"])
    sp.add_argument("--count", type=int)
    sp.set_defaults(func=cmd_session)

    sp = sub.add_parser("kb", help="知识库")
    sp.add_argument("action", nargs="?", default="list",
                    choices=["list", "add", "search", "delete"])
    sp.add_argument("path", nargs="?", default="")
    sp.add_argument("--query")
    sp.add_argument("--key")
    sp.set_defaults(func=cmd_kb)

    sp = sub.add_parser("memory", help="记忆")
    sp.add_argument("action", nargs="?", default="list",
                    choices=["list", "add", "search", "delete", "clear"])
    sp.add_argument("text", nargs="*")
    sp.add_argument("--query")
    sp.add_argument("--kind", choices=["user", "self", "event"])
    sp.add_argument("--key")
    sp.set_defaults(func=cmd_memory)

    sp = sub.add_parser("paths", help="显示路径")
    sp.set_defaults(func=cmd_paths)

    sp = sub.add_parser("models", help="列出模型")
    sp.set_defaults(func=cmd_models)

    sp = sub.add_parser("list-models", help="列出模型")
    sp.set_defaults(func=cmd_list_models)

    sp = sub.add_parser("tools", help="列出工具")
    sp.set_defaults(func=cmd_tools)

    sp = sub.add_parser("config", help="查看或修改配置")
    sp.add_argument("key", nargs="?")
    sp.add_argument("value", nargs="?")
    sp.set_defaults(func=cmd_config)

    sp = sub.add_parser("history", help="查看历史")
    sp.add_argument("--session")
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(func=cmd_history)

    sp = sub.add_parser("tapall", help="全部历史对话，tapall <序号> 继续")
    sp.add_argument("index", nargs="?")
    sp.set_defaults(func=cmd_tapall)

    sp = sub.add_parser("mood", help="心情状态与时间线")
    sp.add_argument("action", nargs="?",
                    choices=["show", "reset", "on", "off"], default="show")
    sp.set_defaults(func=cmd_mood)

    sp = sub.add_parser("reset", help="清空终端会话上下文")
    sp.set_defaults(func=cmd_reset)

    sp = sub.add_parser("wipe", help="抹除所有数据")
    sp.add_argument("-y", "--yes", action="store_true")
    sp.set_defaults(func=cmd_wipe)

    sp = sub.add_parser("persona", help="人格管理")
    sp.add_argument("text", nargs="*")
    sp.add_argument("--clear", action="store_true")
    sp.set_defaults(func=cmd_persona)

    sp = sub.add_parser("export", help="导出配置")
    sp.add_argument("output", nargs="?")
    sp.set_defaults(func=cmd_export)

    sp = sub.add_parser("import", help="导入配置")
    sp.add_argument("archive")
    sp.set_defaults(func=cmd_import)

    sp = sub.add_parser("reload", help="重新加载配置")
    sp.set_defaults(func=cmd_reload)

    for sh in ("fish", "bash", "zsh"):
        sp = sub.add_parser(f"{sh}-init", help=f"安装 {sh} 集成")
        sp.add_argument("shell", nargs="?", default=sh)
        sp.set_defaults(func=cmd_shell_init)

    sp = sub.add_parser("remove-shell-hook", help="移除 shell 集成")
    sp.add_argument("shell", nargs="?", default="fish")
    sp.set_defaults(func=cmd_shell_remove)

    sp = sub.add_parser("daemon", help="后台服务")
    sp.add_argument("action", nargs="?", default="status",
                    choices=["start", "stop", "restart", "status"])
    sp.set_defaults(func=cmd_daemon)

    sp = sub.add_parser("plugins", help="查看已加载插件")
    sp.add_argument("action", nargs="?", default="list",
                    choices=["list", "enable", "disable"])
    sp.add_argument("name", nargs="?")
    sp.set_defaults(func=cmd_plugins)

    sp = sub.add_parser("stt", help="语音转文字（需可选依赖）")
    sp.set_defaults(func=cmd_stt)

    sp = sub.add_parser("voice", help="语音播报（需可选依赖）")
    sp.set_defaults(func=cmd_voice)

    sp = sub.add_parser("usage", help="token 用量统计")
    sp.add_argument("--days", type=int, default=0)
    sp.set_defaults(func=cmd_usage)

    # ---- 会话与上下文补充 ----
    sp = sub.add_parser("pop", help="移出最近 N 轮")
    sp.add_argument("--session")
    sp.add_argument("--count", type=int, default=1)
    sp.set_defaults(func=cmd_pop)

    sp = sub.add_parser("compact", help="压缩上下文")
    sp.add_argument("--session")
    sp.add_argument("--keep", type=int, default=10)
    sp.set_defaults(func=cmd_compact)

    sp = sub.add_parser("reset-memory", help="清空本次会话记忆")
    sp.add_argument("--session")
    sp.set_defaults(func=cmd_reset_memory)

    sp = sub.add_parser("reset-all-memory", help="清空当前人格全部记忆")
    sp.set_defaults(func=cmd_reset_all_memory)

    sp = sub.add_parser("skills", help="skills 管理")
    sp.add_argument("action", nargs="?", default="list",
                    choices=["list", "dir", "run"])
    sp.add_argument("name", nargs="?")
    sp.set_defaults(func=cmd_skills)

    sp = sub.add_parser("stdio", help="长驻 JSON 行协议")
    sp.set_defaults(func=cmd_stdio)

    sp = sub.add_parser("tool-call", help="直接调用工具")
    sp.add_argument("name")
    sp.add_argument("--args", default="")
    sp.set_defaults(func=cmd_tool_call)

    sp = sub.add_parser("web", help="启动本地 WebUI")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=7789)
    sp.set_defaults(func=cmd_web)

    sp = sub.add_parser("bridge", help="webhook 桥（聊天软件接入）")
    sp.add_argument("--port", type=int, default=7800)
    sp.set_defaults(func=cmd_bridge)

    sp = sub.add_parser("layout", help="数据布局")
    sp.add_argument("--apply", action="store_true")
    sp.set_defaults(func=cmd_layout)

    sp = sub.add_parser("embed", help="向量索引")
    sp.add_argument("action", nargs="?", default="status",
                    choices=["status", "rebuild", "models"])
    sp.set_defaults(func=cmd_embed)

    sp = sub.add_parser("effort", help="思考档位")
    sp.add_argument("level", nargs="?")
    sp.set_defaults(func=cmd_effort)

    sp = sub.add_parser("listen", help="开始语音收听")
    sp.set_defaults(func=cmd_listen)

    sp = sub.add_parser("update-default-kb", help="更新内置知识库")
    sp.set_defaults(func=cmd_update_default_kb)

    return p


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    # 内部协议：shell hook 用，解析在 argparse 之前，避免污染子命令表
    if argv and argv[0] in ("--shell-classify", "--shell-intercept"):
        return _handle_shell_protocol(argv)

    p = build_parser()
    args = p.parse_args(argv)
    if getattr(args, "stdout", False):
        os.environ["NOELLE_NO_COLOR"] = "1"
    if not getattr(args, "cmd", None):
        return cmd_repl(args, "normal")
    try:
        return args.func(args)
    except KeyboardInterrupt:
        out("")
        return 130
    except LLMError as e:
        err(str(e))
        return 1


def _handle_shell_protocol(argv: list[str]) -> int:
    """解析 --shell-classify / --shell-intercept 及其参数。"""
    mode = argv[0]
    rest = argv[1:]
    use_stdin = "--stdin" in rest
    shell = ""
    words: list[str] = []
    skip = False
    for i, a in enumerate(rest):
        if skip:
            skip = False
            continue
        if a == "--shell":
            shell = rest[i + 1] if i + 1 < len(rest) else ""
            skip = True
        elif a in ("--stdin", "--"):
            continue
        else:
            words.append(a)

    text = sys.stdin.read() if use_stdin else " ".join(words)

    from .shell import classify
    verdict = classify(text)

    if mode == "--shell-classify":
        print(verdict)
        # 退出码对齐 miyu shell-hook 约定：1=自然语言（hook 接管），0=放行
        return 1 if verdict == "natural" else 0

    # --shell-intercept
    if verdict != "natural":
        print(text)
        return 0
    ns = argparse.Namespace(message=[text], stdout=False)
    return cmd_ask(ns)


if __name__ == "__main__":
    sys.exit(main())
