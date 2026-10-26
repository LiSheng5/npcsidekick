"""持久聊天记录 + 滚动摘要。

  · 每 NPC 一份 store/{id}_chat.jsonl，逐轮追加（用户/助手各一行）—— 重启不丢
  · 整个提示（系统 + 状态 + 记忆 + 历史 + 玩家这句）用到模型上下文窗口的
    NPC_SUMMARY_RATIO（默认 40%）时 → 回合结束后台把旧轮次压成一段摘要，
    存 store/{id}_summary.json，下次拼上下文时放在最前面，只保留最近
    NPC_SUMMARY_KEEP_RECENT 轮逐字；不再按固定 token 预算砍历史
    （仅 build_history 的硬上限兜底，见那里的 docstring）
  · 摘要只保脉络：重要事实由 remember 落进记忆卡（摘要不替代记忆）
  · LLM 不可用时摘要失败 → 原聊天记录一行不动（降级不丢数据）
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.config import budget_history, context_window, summary_keep_recent, summary_ratio
from core.logging_config import log
from core.paths import require_npc_id, resolve_store
from core.tokens import estimate_tokens    # 估算单源在 core；chatlog.estimate_tokens 仍可用（re-export）

_SUMMARY_PROMPT = (
    "把下面这段 NPC 与玩家的对话压成一段简短摘要（中文，600 字以内）："
    "只保留脉络和已发生的事实，不要编造，不要评价。"
    "优先保留玩家的要求、承诺、关系变化和未完成的事。"
    "如果已经有前情摘要，把它和新增对话合成一段完整摘要。"
)


def summary_limit(store_dir: Optional[Path] = None) -> int:
    """压缩触发阈值（token）= 上下文窗口 × NPC_SUMMARY_RATIO，现读可热切。

    本回合提示的本地 token 估算达到这个数，回合结束就后台压缩旧轮次。
    `store_dir` 传给 context_window —— 页面改的窗口存在各自的 llm_config.json。
    """
    return int(context_window(store_dir) * summary_ratio())


# ── 路径 ─────────────────────────────────────────────────

def chat_path(npc_id: str, store_dir: Optional[Path] = None) -> Path:
    """聊天记录路径；npc_id 非法（路径穿越企图）→ ValueError。"""
    return resolve_store(store_dir) / f"{require_npc_id(npc_id)}_chat.jsonl"


def summary_path(npc_id: str, store_dir: Optional[Path] = None) -> Path:
    """摘要状态路径；npc_id 非法（路径穿越企图）→ ValueError。"""
    return resolve_store(store_dir) / f"{require_npc_id(npc_id)}_summary.json"


def select_recent(turns: List[Dict[str, Any]], token_budget: int) -> List[Dict[str, Any]]:
    """从最新往前取到预算为止（至少保留最新一条），返回按时间正序。

    思考内容也算进预算：它会被一起回传给模型（DeepSeek 带 tools 时强制），
    不算预算就会让上下文悄悄超支。
    """
    if not turns:
        return []
    picked: List[Dict[str, Any]] = []
    used = 0
    for msg in reversed(turns):
        cost = estimate_tokens(str(msg.get("content", "")))
        cost += estimate_tokens(str(msg.get("reasoning") or ""))
        if picked and used + cost > token_budget:
            break
        picked.append(msg)
        used += cost
    picked.reverse()
    return picked


# ── 读写 ─────────────────────────────────────────────────

def append_turn(npc_id: str, user_text: str, assistant_text: str,
                store_dir: Optional[Path] = None, reasoning: str = "") -> None:
    """追加一轮：用户一行 + 助手一行。

    reasoning = 该轮答复的思考内容（DeepSeek 带 tools 时下一轮必须原样回传，
    见 turn.py / build_history 的 reasoning_replay）。空则不写这行字段。
    """
    path = chat_path(npc_id, store_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    assistant: Dict[str, Any] = {"role": "assistant", "content": assistant_text, "ts": now}
    if reasoning:
        assistant["reasoning"] = reasoning
    lines = [
        json.dumps({"role": "user", "content": user_text, "ts": now}, ensure_ascii=False),
        json.dumps(assistant, ensure_ascii=False),
    ]
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def load_turns(npc_id: str, limit: Optional[int] = None,
               store_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """读聊天记录（扁平消息列表）。limit = 取最后 limit 行。

    单行损坏（写入中途断电、坏字节等）→ 跳过并告警，不让一条坏行拖垮整个 NPC。
    """
    path = chat_path(npc_id, store_dir)
    if not path.exists():
        return []
    msgs: List[Dict[str, Any]] = []
    for lineno, raw in enumerate(path.read_bytes().splitlines(), 1):
        try:
            line = raw.decode("utf-8")
        except UnicodeDecodeError:      # 坏字节行也按"坏行"跳过，别炸整份记录
            log.warning("chatlog_bad_line", npc_id=npc_id, line=lineno)
            continue
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            log.warning("chatlog_bad_line", npc_id=npc_id, line=lineno)
            continue
        if not isinstance(record, dict) or not isinstance(record.get("content"), str):
            log.warning("chatlog_bad_record", npc_id=npc_id, line=lineno)
            continue
        reasoning = record.get("reasoning")
        msgs.append({
            "role": record.get("role") if record.get("role") in ("user", "assistant") else "user",
            "content": record["content"],
            "ts": record.get("ts", 0.0),
            "reasoning": reasoning if isinstance(reasoning, str) else "",
        })
    if limit is not None and limit >= 0:
        msgs = msgs[-limit:] if limit else []
    return msgs


def load_summary(npc_id: str, store_dir: Optional[Path] = None) -> Dict[str, Any]:
    """读摘要状态：{summary, covered, updated_at}；缺失/损坏 → 空壳。"""
    empty = {"summary": "", "covered": 0, "updated_at": 0.0}
    path = summary_path(npc_id, store_dir)
    if not path.exists():
        return empty
    try:
        raw = json.loads(path.read_text("utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        log.warning("summary_bad_json", npc_id=npc_id)
        return empty
    if not isinstance(raw, dict):
        return empty
    covered = raw.get("covered", 0)
    updated = raw.get("updated_at", 0.0)
    try:
        updated_at = float(updated) if updated is not None else 0.0
    except (TypeError, ValueError):
        updated_at = 0.0
    return {
        "summary": raw.get("summary", "") if isinstance(raw.get("summary"), str) else "",
        "covered": covered if isinstance(covered, int) and covered >= 0 else 0,
        "updated_at": updated_at,
    }


def _save_summary(npc_id: str, summary: str, covered: int,
                  store_dir: Optional[Path] = None) -> None:
    """原子写摘要（先写 `.tmp` 再 `os.replace`，照 memory.save_card 的写法）——
    后台摘要会和回合读取并发，不能让读到半截文件。"""
    path = summary_path(npc_id, store_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(
        {"summary": summary, "covered": covered, "updated_at": time.time()},
        ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ── 滚动摘要（提示用到窗口 × 比例就把旧轮次压成一段）──────────

# 聊天记录一轮 = 用户 + 助手各一行（jsonl 行数口径），keep_recent 轮 → 行数 ×2
_MESSAGES_PER_TURN = 2
# D7 分片折叠：单片输入上限 = 窗口 × 0.3。老存档积压按轮次切片滚完 ——
# 每片成功就落盘推进 covered，失败停在已完成处，不丢数据。
_FOLD_SLICE_RATIO = 0.3


def _fold_plan(npc_id: str, prompt_tokens: Optional[int] = None,
               keep_recent: Optional[int] = None,
               store_dir: Optional[Path] = None,
               turns: Optional[List[Dict[str, Any]]] = None):
    """摘要计划 → `(turns, state, covered, cutoff)`；不该摘要 / 没得压 → None。纯读。

    触发条件 = 传入的本回合提示 token 数 ≥ 窗口 × NPC_SUMMARY_RATIO，且保留窗口
    之外还有没被摘要覆盖的旧轮次。`prompt_tokens=None`（旧调用方 / 手动触发）→
    不看阈值，只要有可压的旧轮次就压（强制压缩）。
    `maybe_summarize` 与 `needs_summary` 共用这份判断，两处口径永远一致。
    `turns` 可传入已读好的聊天记录（turn.py 的 needs_summary → 后台折叠共享一次
    读盘）；state 每次现读 —— covered 可能被并行摘要推进，小 JSON 不值得缓存。
    """
    keep = summary_keep_recent() if keep_recent is None else max(0, keep_recent)
    if turns is None:
        turns = load_turns(npc_id, store_dir=store_dir)
    if prompt_tokens is not None and prompt_tokens < summary_limit(store_dir=store_dir):
        return None
    state = load_summary(npc_id, store_dir=store_dir)
    covered = min(state["covered"], len(turns))
    cutoff = len(turns) - keep * _MESSAGES_PER_TURN
    if cutoff <= covered:
        return None                       # 没有可压的旧轮次
    return turns, state, covered, cutoff


def summary_plan(npc_id: str, prompt_tokens: Optional[int] = None,
                 keep_recent: Optional[int] = None,
                 store_dir: Optional[Path] = None):
    """公开的摘要计划（turn.py 拿它决定调度，并把 turns 传给后台折叠，共享读盘）。"""
    return _fold_plan(npc_id, prompt_tokens, keep_recent, store_dir)


def needs_summary(npc_id: str, prompt_tokens: Optional[int] = None,
                  keep_recent: Optional[int] = None,
                  store_dir: Optional[Path] = None,
                  turns: Optional[List[Dict[str, Any]]] = None) -> bool:
    """现在该不该摘要（提示 token ≥ 窗口 × 比例，且有可压的旧轮次）？纯读、不惊动 LLM。

    回合结束用它决定要不要后台触发摘要（turn.py）；`prompt_tokens` =
    本回合提示的本地 token 估算，拿不到传 None = 不看阈值（强制压缩口径）。
    """
    return _fold_plan(npc_id, prompt_tokens, keep_recent, store_dir, turns=turns) is not None


def _turn_line(msg: Dict[str, Any]) -> str:
    """一轮消息 → 摘要输入里的一行。"""
    return f"{'玩家' if msg['role'] == 'user' else 'NPC'}：{msg['content']}"


def _take_slice(turns: List[Dict[str, Any]], start: int, end: int,
                budget: int) -> Tuple[List[Dict[str, Any]], int]:
    """从 turns[start:end] 按轮次（消息对）取一片：各行估算合计 ≤ budget，至少取一轮。

    返回 (片内消息, 下一游标)。单轮就超预算时也必须取 —— 否则原地卡死；
    这种巨型单轮只此一次，日志由调用方记。
    """
    picked: List[Dict[str, Any]] = []
    used = 0
    cursor = start
    while cursor < end:
        pair = turns[cursor:cursor + _MESSAGES_PER_TURN]
        cost = sum(estimate_tokens(_turn_line(m)) for m in pair)
        if picked and used + cost > budget:
            break
        picked.extend(pair)
        used += cost
        cursor += len(pair)
    return picked, cursor


def _summary_effort(store_dir: Optional[Path] = None) -> Optional[str]:
    """摘要调用显式关思考（不值得为摘要烧思考 token）。

    尊重「该模型不认思考参数」（thinking_unsupported）→ 一个参数都不发（None），
    否则显式发 off。
    """
    from core import llm_runtime

    if llm_runtime.llm_settings(store_dir=store_dir).get("thinking_unsupported"):
        return None
    return "off"


def maybe_summarize(npc_id: str, llm, prompt_tokens: Optional[int] = None,
                    keep_recent: Optional[int] = None,
                    store_dir: Optional[Path] = None,
                    turns: Optional[List[Dict[str, Any]]] = None) -> Optional[str]:
    """提示 token ≥ 窗口 × 比例时把最旧的轮次压成摘要；`prompt_tokens=None` = 强制压缩
    （不看阈值，只要有可压的旧轮次就压）。未到阈值/无内容可压/失败 → None。

    D7：积压按轮次切片、滚动折叠 —— 每片输入 ≤ 窗口 × 0.3，上一片的摘要作为
    「前情摘要」进下一片；每片成功就落盘推进 covered，中途失败停在已完成处。
    聊天记录一行不动、不丢数据。成功返回最新摘要文本（至少一片成功）。
    """
    plan = _fold_plan(npc_id, prompt_tokens, keep_recent, store_dir, turns=turns)
    if plan is None:
        return None
    turns_all, state, covered, cutoff = plan

    slice_cap = max(1, int(context_window(store_dir) * _FOLD_SLICE_RATIO))
    summary_text = state["summary"]
    cursor = covered
    folded_any = False
    while cursor < cutoff:
        head = f"前情摘要：{summary_text}" if summary_text else ""
        budget = slice_cap - (estimate_tokens(head) if head else 0)
        chunk, next_cursor = _take_slice(turns_all, cursor, cutoff, max(1, budget))
        if not chunk:
            break
        payload = ([head] if head else []) + [_turn_line(m) for m in chunk]
        try:
            resp = llm.chat([
                {"role": "system", "content": _SUMMARY_PROMPT},
                {"role": "user", "content": "\n".join(payload)},
            ], reasoning_effort=_summary_effort(store_dir))
        except Exception as exc:               # LLM 不可用 → 降级，不丢数据
            log.warning("summary_failed", npc_id=npc_id, covered=cursor,
                        error=str(exc)[:120])
            break
        summary = (getattr(resp, "content", "") or "").strip()
        if not summary:
            log.warning("summary_empty", npc_id=npc_id, covered=cursor)
            break
        summary_text = summary
        cursor = next_cursor
        folded_any = True
        _save_summary(npc_id, summary_text, cursor, store_dir=store_dir)
        log.info("summary_updated", npc_id=npc_id, covered=cursor, folded=len(chunk))
    return summary_text if folded_any else None


def _history_message(msg: Dict[str, Any], reasoning_replay: bool) -> Dict[str, Any]:
    """一轮聊历史 → 一条 messages 项。

    reasoning_replay=True（DeepSeek 带 tools）时，assistant 的历史思考内容必须
    以 `reasoning_content` 原样带回，否则服务端 400；其它 Provider 不带这个字段。
    """
    out: Dict[str, Any] = {"role": msg["role"], "content": msg["content"]}
    reasoning = msg.get("reasoning") or ""
    if reasoning_replay and msg["role"] == "assistant" and reasoning:
        out["reasoning_content"] = reasoning
    return out


def build_history(npc_id: str, llm, token_budget: Optional[int] = None,
                  store_dir: Optional[Path] = None,
                  summarize: bool = True,
                  reasoning_replay: bool = False) -> List[Dict[str, Any]]:
    """拼上下文用的历史：可选的摘要消息 + 未被摘要覆盖的**全部**聊天记录。

    历史本身不按固定预算截断（盲区已消除）；`token_budget` 是硬上限
    （默认 `config.budget_history()` = 窗口 × 0.7），只防摘要失败（LLM 不可用）
    时历史撑爆上下文，正常不会触发。

    `summarize=True`（默认）= 旧行为：拼历史前同步**强制**摘要一次（不看阈值）；
    turn.py 传 `False` —— 摘要移出回合关键路径，改在回合结束后台按
    本回合提示 token 触发，回合内只读现成的摘要状态。

    `reasoning_replay=True` 时把落盘的思考内容作为 `reasoning_content` 带回
    （DeepSeek 带 tools 的硬要求；见 ProviderProtocol.replays_reasoning_content）。
    """
    if summarize:
        maybe_summarize(npc_id, llm, store_dir=store_dir)   # 强制压缩（不看阈值）
    state = load_summary(npc_id, store_dir=store_dir)
    turns = load_turns(npc_id, store_dir=store_dir)
    covered = min(state["covered"], len(turns))
    if token_budget is None:
        token_budget = budget_history(store_dir=store_dir)   # 对话层硬上限（core/config，单源）

    history: List[Dict[str, Any]] = []
    if state["summary"]:
        history.append({"role": "assistant",
                        "content": f"（此前对话摘要，供你参考）{state['summary']}"})
    history.extend(_history_message(m, reasoning_replay)
                   for m in select_recent(turns[covered:], token_budget))
    return history
