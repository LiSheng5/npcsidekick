"""持久聊天记录 + 滚动摘要。

  · 每 NPC 一份 store/{id}_chat.jsonl，逐轮追加（用户/助手各一行）—— 重启不丢
  · 上下文超阈值（默认 20K token，可配）→ 把最旧的轮次让 LLM 压成一段摘要，
    存 store/{id}_summary.json，下次拼上下文时放在最前面
  · 摘要只保脉络：重要事实由 remember 落进记忆卡（摘要不替代记忆）
  · LLM 不可用时摘要失败 → 原聊天记录一行不动（降级不丢数据）
"""
from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.logging_config import log

# 运行时数据目录（工程根/store；测试用 monkeypatch 替换）
STORE_DIR = Path(__file__).resolve().parent / "store"

# 阈值与保留窗口（按估算的绝对 token 数，默认 20K，可配；现读环境变量可热切）
DEFAULT_TOKEN_THRESHOLD = 20000
DEFAULT_KEEP_RECENT_TURNS = 8
_ENV_THRESHOLD = "NPC_SUMMARY_TOKEN_THRESHOLD"
_ENV_KEEP_RECENT = "NPC_SUMMARY_KEEP_RECENT"

_SUMMARY_PROMPT = (
    "把下面这段 NPC 与玩家的对话压成一段简短摘要（中文，200 字以内）："
    "只保留脉络和已发生的事实，不要编造，不要评价。"
    "如果已经有前情摘要，把它和新增对话合成一段完整摘要。"
)

# CJK 与 CJK 标点的码点区间（token 估算用）
_CJK_RANGES = ((0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF), (0x3000, 0x303F))


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return max(0, int(float(str(raw).strip())))
    except ValueError:
        return default


def token_threshold() -> int:
    """摘要触发阈值（token），现读环境变量。"""
    return _env_int(_ENV_THRESHOLD, DEFAULT_TOKEN_THRESHOLD)


def keep_recent_turns() -> int:
    """摘要后仍逐字保留的最近轮数，现读环境变量。"""
    return _env_int(_ENV_KEEP_RECENT, DEFAULT_KEEP_RECENT_TURNS)


# ── 路径 ─────────────────────────────────────────────────

def chat_path(npc_id: str) -> Path:
    return STORE_DIR / f"{npc_id}_chat.jsonl"


def summary_path(npc_id: str) -> Path:
    return STORE_DIR / f"{npc_id}_summary.json"


# ── token 估算（纯本地确定性，只用于量级判断，不求精确）─────

def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return any(lo <= code <= hi for lo, hi in _CJK_RANGES)


def estimate_tokens(text: str) -> int:
    """CJK 字符按 1 token、其余按 4 字符 1 token 估算。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if _is_cjk(ch))
    return cjk + math.ceil((len(text) - cjk) / 4)


def select_recent(turns: List[Dict[str, Any]], token_budget: int) -> List[Dict[str, Any]]:
    """从最新往前取到预算为止（至少保留最新一条），返回按时间正序。"""
    if not turns:
        return []
    picked: List[Dict[str, Any]] = []
    used = 0
    for msg in reversed(turns):
        cost = estimate_tokens(str(msg.get("content", "")))
        if picked and used + cost > token_budget:
            break
        picked.append(msg)
        used += cost
    picked.reverse()
    return picked


# ── 读写 ─────────────────────────────────────────────────

def append_turn(npc_id: str, user_text: str, assistant_text: str) -> None:
    """追加一轮：用户一行 + 助手一行。"""
    path = chat_path(npc_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    lines = [
        json.dumps({"role": "user", "content": user_text, "ts": now}, ensure_ascii=False),
        json.dumps({"role": "assistant", "content": assistant_text, "ts": now}, ensure_ascii=False),
    ]
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def load_turns(npc_id: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """读聊天记录（扁平消息列表）。limit = 取最后 limit 行。

    单行损坏（写入中途断电等）→ 跳过并告警，不让一条坏行拖垮整个 NPC。
    """
    path = chat_path(npc_id)
    if not path.exists():
        return []
    msgs: List[Dict[str, Any]] = []
    for lineno, line in enumerate(path.read_text("utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            log.warning("chatlog_bad_line", npc_id=npc_id, line=lineno)
            continue
        if not isinstance(raw, dict) or not isinstance(raw.get("content"), str):
            log.warning("chatlog_bad_record", npc_id=npc_id, line=lineno)
            continue
        msgs.append({
            "role": raw.get("role") if raw.get("role") in ("user", "assistant") else "user",
            "content": raw["content"],
            "ts": raw.get("ts", 0.0),
        })
    if limit is not None and limit >= 0:
        msgs = msgs[-limit:] if limit else []
    return msgs


def load_summary(npc_id: str) -> Dict[str, Any]:
    """读摘要状态：{summary, covered, updated_at}；缺失/损坏 → 空壳。"""
    empty = {"summary": "", "covered": 0, "updated_at": 0.0}
    path = summary_path(npc_id)
    if not path.exists():
        return empty
    try:
        raw = json.loads(path.read_text("utf-8"))
    except json.JSONDecodeError:
        log.warning("summary_bad_json", npc_id=npc_id)
        return empty
    if not isinstance(raw, dict):
        return empty
    covered = raw.get("covered", 0)
    return {
        "summary": raw.get("summary", "") if isinstance(raw.get("summary"), str) else "",
        "covered": covered if isinstance(covered, int) and covered >= 0 else 0,
        "updated_at": float(raw.get("updated_at", 0.0) or 0.0),
    }


def _save_summary(npc_id: str, summary: str, covered: int) -> None:
    path = summary_path(npc_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        {"summary": summary, "covered": covered, "updated_at": time.time()},
        ensure_ascii=False, indent=2), encoding="utf-8")


# ── 滚动摘要（超阈值就把最旧的轮次压成一段）────────────────

def maybe_summarize(npc_id: str, llm, keep_recent: Optional[int] = None) -> Optional[str]:
    """超阈值时把最旧的轮次压成摘要；未超阈值/无内容可压/失败 → None。

    成功返回新摘要文本并落盘（covered 推进）；失败不写任何东西，
    聊天记录一行不动。
    """
    threshold = token_threshold()
    keep = keep_recent_turns() if keep_recent is None else max(0, keep_recent)

    turns = load_turns(npc_id)
    total = sum(estimate_tokens(m["content"]) for m in turns)
    if total <= threshold:
        return None

    state = load_summary(npc_id)
    covered = min(state["covered"], len(turns))
    cutoff = len(turns) - keep * 2
    if cutoff <= covered:
        return None                       # 没有可压的旧轮次

    old = turns[covered:cutoff]
    payload = []
    if state["summary"]:
        payload.append(f"前情摘要：{state['summary']}")
    payload.extend(f"{'玩家' if m['role'] == 'user' else 'NPC'}：{m['content']}" for m in old)

    try:
        resp = llm.chat([
            {"role": "system", "content": _SUMMARY_PROMPT},
            {"role": "user", "content": "\n".join(payload)},
        ])
    except Exception as exc:               # LLM 不可用 → 降级，不丢数据
        log.warning("summary_failed", npc_id=npc_id, error=str(exc)[:120])
        return None

    summary = (getattr(resp, "content", "") or "").strip()
    if not summary:
        log.warning("summary_empty", npc_id=npc_id)
        return None

    _save_summary(npc_id, summary, cutoff)
    log.info("summary_updated", npc_id=npc_id, covered=cutoff, folded=len(old))
    return summary


def build_history(npc_id: str, llm, token_budget: Optional[int] = None) -> List[Dict[str, Any]]:
    """拼上下文用的历史：可选的摘要消息 + 最近若干轮（受 token 预算约束）。"""
    maybe_summarize(npc_id, llm)
    state = load_summary(npc_id)
    turns = load_turns(npc_id)
    covered = min(state["covered"], len(turns))
    budget = token_threshold() if token_budget is None else token_budget

    history: List[Dict[str, Any]] = []
    if state["summary"]:
        history.append({"role": "assistant",
                        "content": f"（此前对话摘要，供你参考）{state['summary']}"})
    history.extend({"role": m["role"], "content": m["content"]}
                   for m in select_recent(turns[covered:], budget))
    return history
