"""mood 插件：给 noelle 一份看得见的心情。

方法移植自社区「大肥鱼情感插件」（dsh-feiyu-mood，MIT）的机制：

- 心情值 0–100，基线 70；无新情绪时按半衰期 3 小时回落基线
- 七根情绪（喜 怒 哀 惧 爱 恶 欲）各 -100..100，
  按半衰期 90 分钟向 0 衰减
- 每轮对话后把「用户说的 + noelle 答的」交给模型判定：
  涨跌 + 七情变化 + 一句理由，记入时间线
- 下一轮开始前把此刻心情注入上下文，语气真的受影响

与原插件一样：数据全在本机 SQLite，判定走自己配的模型。
"""

from __future__ import annotations

import json
import math
import time

EMOTIONS = ("喜", "怒", "哀", "惧", "爱", "恶", "欲")
BASELINE = 70
MOOD_HALF_LIFE = 3 * 3600       # 心情回落基线
EMO_HALF_LIFE = 90 * 60         # 七情回落 0


def _decay(value: float, half_life: float, elapsed: float) -> float:
    if elapsed <= 0 or value == 0:
        return value
    return value * (0.5 ** (elapsed / half_life))


def current_state(db) -> dict:
    """惰性衰减后的当前心情状态（不写库）"""
    row = db.conn().execute(
        "SELECT * FROM mood_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    now = time.time()
    if row is None:
        return {"mood": BASELINE, "delta": 0,
                "emotions": {e: 0 for e in EMOTIONS}, "ts": 0, "reason": ""}
    elapsed = now - row["ts"]
    mood = row["mood"]
    # 心情向基线回落
    mood = BASELINE + _decay(mood - BASELINE, MOOD_HALF_LIFE, elapsed)
    emotions = json.loads(row["emotions"])
    for k in emotions:
        emotions[k] = _decay(emotions[k], EMO_HALF_LIFE, elapsed)
    return {"mood": int(round(mood)), "delta": row["delta"],
            "emotions": {k: int(round(v)) for k, v in emotions.items()},
            "ts": row["ts"], "reason": row["reason"]}


def face(mood: int) -> str:
    """五档表情"""
    if mood >= 85:
        return "兴高采烈 （＾▽＾）"
    if mood >= 70:
        return "心情不错 （￣▽￣）"
    if mood >= 50:
        return "平静如水 （・_・）"
    if mood >= 30:
        return "有点低落 （´・ω・`）"
    return "相当糟糕 （＞﹏＜）"


def inject_block(state: dict) -> str:
    """注入上下文的心情段"""
    emo = " ".join(f"{k}{v:+d}" for k, v in state["emotions"].items()
                   if v != 0) or "无波动"
    return (f"[当前心情] {state['mood']}/100 {face(state['mood'])}"
            f" · {emo} · {state['reason']}")


JUDGE_PROMPT = (
    "你是情绪判定器。根据这轮对话判断 AI 的心情变化，输出 JSON（不要多余文字）：\n"
    '{"delta": <心情涨跌 -20..20>,'
    ' "emotions": {"喜": <变化 -30..30>, "怒": .., "哀": .., "惧": ..,'
    ' "爱": .., "恶": .., "欲": ..},'
    ' "reason": "<一句话理由>"}\n'
    "规则：被夸/被感谢/聊得开心 → 正；被骂/被无视/被命令干活 → 负；"
    "中性任务交流 → 0 附近；只输出变化大的一两个情绪，其余省略。"
)


def judge(agent, user_text: str, reply_text: str) -> dict | None:
    """判定一轮对话的心情变化并落库。失败返回 None。"""
    from .agent import _json_slice
    from .llm import Message
    from .storage import get_db
    try:
        comp = agent.client().complete([
            Message("system", JUDGE_PROMPT),
            Message("user",
                    f"用户说：{user_text[:400]}\nAI答：{reply_text[:400]}"),
        ], temperature=0.1)
        data = json.loads(_json_slice(comp.text))
    except Exception:  # noqa: BLE001
        return None

    db = get_db()
    state = current_state(db)
    delta = max(-20, min(20, int(data.get("delta", 0) or 0)))
    mood = max(0, min(100, state["mood"] + delta))
    emotions = dict(state["emotions"])
    for k, v in (data.get("emotions") or {}).items():
        if k in emotions:
            emotions[k] = max(-100, min(100, emotions[k] + int(v or 0)))
    reason = str(data.get("reason", ""))[:100]
    db.conn().execute(
        "INSERT INTO mood_log(ts,mood,delta,emotions,reason) VALUES(?,?,?,?,?)",
        (time.time(), mood, delta,
         json.dumps(emotions, ensure_ascii=False), reason),
    )
    db.conn().commit()
    return {"mood": mood, "delta": delta, "emotions": emotions, "reason": reason}


def timeline(db, limit: int = 10) -> list[dict]:
    rows = db.conn().execute(
        "SELECT * FROM mood_log ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def reset(db) -> None:
    db.conn().execute("DELETE FROM mood_log")
    db.conn().commit()
