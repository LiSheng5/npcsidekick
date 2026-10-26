"""回合主体 —— 一次 /api/talk 的 LLM ↔ 工具多轮循环（产出 SSE 帧）。

执行模型（2026-09-28 起）：**动作 = LLM 直接调用的工具**。
  记忆工具由本进程执行；动作工具由本模块**同步调用** mod 的 `execute_url`（协议 §3），
  结果作为工具消息回上下文继续推理，并**当场写进记忆卡** —— mod 不用回报。

  结果没定的两种情况（`running` / 同步等待超时 `unknown`）**不写卡**，改为登记 `pending`
  （见 `pending.py`），等 mod 带 `call_id` 回报时再写；TTL 到点仍没回报才算没做成。

两条通道严格分离（台词只走 delta 帧，流末尾的 action 帧只是"已执行动作"的记录）：
  · 台词通道 = SSE 的 delta 帧，只有角色说的话
  · 结构化通道 = action 帧，在流末尾，mod **不需要**照它做任何事（仅记录/展示）
  · 语音通道 = audio 帧（仅 voice=true 时），在 done 之前，缺依赖/超时则不出帧

同步文件 IO（记忆卡读写 / 聊天记录读写）一律 `asyncio.to_thread` ——
本函数是 SSE 生成器，跑在事件循环上，直接调用会把整个服务器卡住。
唯一例外：客户端断连 / 取消时的落盘兜底（文末 `finally`）—— 那种挂起状态下不能再 await。
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional, Tuple

from core.client import LLMClient
from core import config
from core.config_flags import env_flag, env_num, env_text
from core.llm_runtime import short_err, thinking_effort
from core.logging_config import log
from core.personas import rule_reply
from core.tokens import estimate_tokens

import chatlog
import context
import memory
import pending
import tools
import tts

# 单次对话内的工具调用轮次上限（记忆 + 动作共用这个预算，防跑飞）
MAX_TOOL_ROUNDS = 3

# 记忆检索返回条数（recall 工具 + 回合开始的记忆预取共用这个 top-k）
RECALL_TOP_K = 5

# T4·remember 静默收尾开关（家规：现读环境变量、可热切）。
# 开：一轮里只有 remember 且台词已出 → 当场收尾，不再多跑一轮逼台词；
# 关：保持旧节奏（工具轮之后总是再跑一轮拿台词）。
_ENV_SILENT_REMEMBER = "NPC_SILENT_REMEMBER"

# T6·动作参数校验开关（家规：现读环境变量、可热切）。
# 开：执行前按声明校验（D9）—— 非法参数不下发给 mod，回灌错误让模型重发；
# 关：老行为（声明只当说明，拿到什么发什么）。
_ENV_VALIDATE_ACTION_ARGS = "NPC_VALIDATE_ACTION_ARGS"


def silent_remember_enabled() -> bool:
    """remember 静默收尾开关（现读，可热切）：NPC_SILENT_REMEMBER，默认开。"""
    return env_flag(_ENV_SILENT_REMEMBER, True)


def validate_action_args_enabled() -> bool:
    """动作参数校验开关（现读，可热切）：NPC_VALIDATE_ACTION_ARGS，默认关（先关后开）。"""
    return env_flag(_ENV_VALIDATE_ACTION_ARGS, False)


def _sse(payload: Dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


# ── 回合 trace ────────────────────────────────────────────
# 每回合结束输出一条 `turn_trace` 结构化日志（含降级回合）：
# 记什么、不记什么见各字段说明 —— 默认**只记长度不记原文**（隐私 + 日志体积）。
# NPC_TRACE_FILE 非空时同一条记录再以 JSONL 追加写入该文件；写失败只告警，不影响回合。

_ENV_TRACE_FILE = "NPC_TRACE_FILE"


def _emit_turn_trace(record: Dict[str, Any]) -> None:
    """输出一条 turn_trace：结构化日志 + 可选 JSONL 落盘。全程不抛、不影响回合。"""
    try:
        log.info("turn_trace", **record)
    except Exception as exc:                       # 日志坏了也不能连累回合
        log.warning("turn_trace_log_failed", error=short_err(exc))
    path = env_text(_ENV_TRACE_FILE, "").strip()
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:
        log.warning("turn_trace_write_failed", path=path, error=short_err(exc))


def _trace_tool(name: str, capabilities: Optional[List[dict]], ok: bool,
                status: str, ms: float) -> Dict[str, Any]:
    """trace 的一条工具记录。`status` 如实保留执行侧取值（done/running/unknown/failed、
    memory 的 success/error/rejected，以及 bad_args / ignored / deduped / invalid_args）。"""
    return {
        "name": name,
        "kind": "action" if tools.is_action_tool(name, capabilities) else "memory",
        "ok": bool(ok),
        "status": status,
        "ms": round(ms, 1),
    }


@dataclass
class _TurnTrace:
    """一回合的观测记录（turn_trace 那条日志的载体）。

    此前是 stream_turn 里 9 个散装局部变量 + 收尾闭包拼装，现在收拢成一个对象：
    各阶段就地更新，收尾 `_emit_turn_trace` 一次性出账。
    """

    t_start: float                      # 回合开始（含等锁）—— 全部耗时的基准
    rounds: int = 0                     # 实际发起的 LLM 轮数
    tools: List[Dict[str, Any]] = field(default_factory=list)  # {name, kind, ok, status, ms}
    llm_fallback: bool = False          # 是否走了规则回复
    summarized: bool = False            # 本回合是否触发了摘要（启动了后台摘要）
    ended_early: bool = False           # T4：静默收尾触发（只有静默调用且台词已出 → 提前收尾）
    tokens: Dict[str, int] = field(default_factory=lambda: {
        "system": 0, "history": 0, "state": 0, "memory": 0, "user": 0, "tools": 0})
    mem_hits: int = 0                   # 记忆预取进块条数（含 pinned）
    mem_filtered: int = 0               # 记忆预取没进块条数（action 类 + 预算外的）
    first_delta_at: Optional[float] = None

    def note_first_delta(self) -> None:
        """首帧台词的时间戳（只记一次）。"""
        if self.first_delta_at is None:
            self.first_delta_at = time.perf_counter()

    def add_tool(self, name: str, capabilities: Optional[List[dict]], ok: bool,
                 status: str, ms: float) -> None:
        self.tools.append(_trace_tool(name, capabilities, ok, status, ms))

    def record(self, npc_id: str, message: str, run: "_TurnRun",
               store_dir: Optional[Path] = None) -> Dict[str, Any]:
        """拼 turn_trace 记录（发出时现算耗时；tokens.total = 各层之和）。"""
        return {
            "npc_id": npc_id,
            "rounds": self.rounds,
            "llm_fallback": self.llm_fallback,
            "summarized": self.summarized,
            "ended_early": self.ended_early,
            "no_dialogue": not run.dialogue_emitted,
            "tools": self.tools,
            "tokens": {**self.tokens, "total": sum(self.tokens.values())},
            # 压缩触发对照：本回合提示 token（本地估算）vs 阈值（窗口 × 比例）
            "summary_trigger": {"prompt_tokens": sum(self.tokens.values()),
                                "limit": chatlog.summary_limit(store_dir=store_dir)},
            "memory_prefetch": {"hits": self.mem_hits, "filtered": self.mem_filtered},
            "first_token_ms": (None if self.first_delta_at is None
                               else round((self.first_delta_at - self.t_start) * 1000, 1)),
            "total_ms": round((time.perf_counter() - self.t_start) * 1000, 1),
            "message_len": len(message),
            "reply_len": len("".join(run.full_parts)),
        }


@dataclass
class _TurnRun:
    """回合可变状态 —— 装配 / 工具循环 / 收尾三段共用的那几个袋子。"""

    full_parts: List[str] = field(default_factory=list)   # 台词全文（落盘 / 语音 / trace）
    executed: List[Dict[str, Any]] = field(default_factory=list)  # 已执行动作（action 帧）
    dialogue_emitted: bool = False                        # 是否吐过台词（delta 帧）
    recorded: bool = False                                # 聊天记录是否已落盘（finally 兜底用）
    turn_reasoning: str = ""     # 最终答复那一轮的思考内容（落盘，下一回合回传）


# ── 流式工具调用累积 ─────────────────────────────────────

class _ToolCallBuffer:
    """把跨 chunk 的 tool_call 增量拼成完整调用。"""

    def __init__(self) -> None:
        self.name = ""
        self.arguments = ""
        self.call_id = ""
        self.bad_args = False     # 参数不是合法 JSON 对象 —— 不该拿去执行，要回给模型重发

    def feed(self, delta: Dict[str, Any]) -> None:
        if delta.get("id"):
            self.call_id = str(delta["id"])
        if delta.get("function_name"):
            self.name += str(delta["function_name"])
        if delta.get("function_arguments"):
            self.arguments += str(delta["function_arguments"])

    def finalize(self, index: int) -> Dict[str, Any]:
        """拼好的调用：`{id, name, args}`；参数不是合法 JSON 对象时**带** `bad_args=True` 标记。

        坏参数的调用绝不拿去执行（空参数执行 = 拿玩家的游戏世界瞎赌），
        回合循环看到标记只回一条工具消息让模型自己重发。
        """
        raw = self.arguments.strip()
        args: Any = {}
        if raw:
            try:
                args = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("tool_args_bad_json", tool=self.name, raw=self.arguments[:120])
                self.bad_args = True
        if not isinstance(args, dict):
            args = {}
            self.bad_args = True
        call: Dict[str, Any] = {"id": self.call_id or f"call_{index + 1}",
                                "name": self.name, "args": args}
        if self.bad_args:
            call["bad_args"] = True
        return call


# ── 动作执行：同步调用 mod 的 execute_url（协议 §3）──────────

async def _http_execute(url: str, payload: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    """默认执行器：POST 给 mod 的 execute_url，返回 JSON 对象（失败由调用方统一处理）。"""
    import httpx

    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()
        data = response.json()
    return data if isinstance(data, dict) else {}


async def invoke_action(execute_url: str, npc_id: str, name: str, args: Dict[str, Any],
                        mod: Optional[str], timeout: float,
                        executor: Optional[Any] = None,
                        pending_actions: Optional[pending.PendingActions] = None) -> Dict[str, Any]:
    """同步请求 mod 执行一个动作，返回统一形态 `{ok, status, note, call_id}`。

    - 连不上 / 响应非法 → `ok=False` 的失败（请求大概率没到 mod），**绝不假装做成**（协议 §3）
    - **超时** → `status="unknown"`：请求可能已经到 mod 并被执行了，只是没等到回应，
      故不当作失败，由调用方登记待定（`pending`）等回报，TTL 到点再算账
    - **重复动作保护**：`pending_actions` 里已有同 NPC 同名同参的在途条目
      （`running` / `unknown`）→ 不发给 mod，直接回 `status="deduped"`（调用方据此
      不写卡、不进 action 帧）。LLM 重发同一动作会生成新的 `call_id`，幂等拦不住，
      只能按名 + 参识别；参数不同 = 新指令，正常放行
    - `status`：`done`（已做完，默认）/ `running`（mod 说还在做）/ `unknown`（超时未定）
      / `failed`（大脑侧失败 —— 只给超时以外那些确定没做成的）/ `deduped`（重复，没下发）。
      mod 回应里的 status 只认 `done` / `running`（`failed` 按失败收、ok 压成 False）；
      别的取值是协议误用：告警后按 `failed` 处理，**绝不静默当 done 记完成**
    - `call_id`：本次调用的关联 id（`uuid4().hex`，随请求发给 mod，Mod 回报时原样带回）——
      不复用 LLM 的 tool_call id，那个不保证唯一
    - `executor` 可注入（测试用假实现）；缺省走真实 HTTP（`_http_execute`）
    """
    if pending_actions is not None:
        dup = pending_actions.find_in_flight(npc_id, name, args)
        if dup is not None:
            log.warning("action_deduped", npc_id=npc_id, action=name,
                        state=dup.get("state"), call_id=dup.get("call_id"))
            return {"ok": False, "status": "deduped", "call_id": "",
                    "note": "这件事还在进行或待确认，这次没有重复下达"}
    call_id = pending.new_call_id()
    payload = {"npc_id": npc_id, "action": name, "params": args, "mod": mod,
               "call_id": call_id}
    runner = executor or _http_execute
    try:
        data = await asyncio.wait_for(runner(execute_url, payload, timeout), timeout=timeout)
    except asyncio.TimeoutError:
        log.warning("action_execute_timeout", npc_id=npc_id, action=name,
                    call_id=call_id, timeout_s=timeout)
        return {"ok": False, "status": "unknown", "call_id": call_id,
                "note": f"等待超过 {timeout:g}s 仍未返回"}
    except Exception as exc:
        log.warning("action_execute_failed", npc_id=npc_id, action=name,
                    call_id=call_id, error=type(exc).__name__)
        return {"ok": False, "status": "failed", "call_id": call_id,
                "note": f"执行接口没接上（{type(exc).__name__}）"}
    if not isinstance(data, dict):
        return {"ok": False, "status": "failed", "call_id": call_id,
                "note": "执行接口回了非法响应"}
    ok = bool(data.get("ok"))
    status = str(data.get("status") or "done").strip().lower()
    if status == "failed":
        ok = False                 # mod 自己报失败就别记"完成"（ok 与 status 打架取保守侧）
    elif status not in ("done", "running"):
        # 协议误用（未知取值）：告警并按"没做成"落点，绝不静默当 done 记完成（协议 §3）
        log.warning("action_status_unknown", npc_id=npc_id, action=name,
                    call_id=call_id, status=status[:32])
        status, ok = "failed", False
    return {"ok": ok, "status": status, "call_id": call_id,
            "note": str(data.get("note") or "").strip()}


def action_tool_text(name: str, outcome: Dict[str, Any]) -> str:
    """动作结果 → 给 LLM 的工具消息文本（进上下文的，不是写卡文案）。"""
    note = str(outcome.get("note") or "").strip()
    status = outcome.get("status")
    if status == "running":
        return f"{name} 还在进行中，结果稍后才出来"
    if status == "unknown":
        # 同步等待超时：mod 可能已经做了 —— 只说"不知道"，别替它下结论
        return f"{name} 没等到回应，不确定是否已经做成"
    if status == "deduped":
        # 重复动作保护：同名同参的动作还在进行/待确认，这次没重复下发
        return note or "这件事还在进行或待确认，这次没有重复下达"
    if outcome.get("ok"):
        return note or f"{name} 已完成"
    return f"没做成: {note or name}"


# ── 后台摘要（摘要移出关键路径）────────────────────────────
# 回合结束、`append_turn` 之后后台触发 `maybe_summarize` —— 摘要 LLM 不再挡玩家
# 看到第一个字；摘要期间的新回合照常读旧摘要状态，由历史硬上限（窗口 × 0.7）
# 兜底保证上下文有界。
# · 任务引用放 scheduler.tasks，跑完移除 —— 不留引用会被垃圾回收半路取消
# · 同一 NPC 同时只允许一个摘要在跑（summarizing 记正在摘要的 npc_id，跑完移除）
# · 异常只告警，不上抛（_run 内部已吞 LLM 错误，外面兜别的）
# 调度状态收在 `SummaryScheduler` 对象里、每 app 一份（server 注入）——
# 不再是模块级全局，多 app 实例 / 并行测试互不干扰。

class SummaryScheduler:
    """后台摘要的调度账本：同 NPC 串行 + 任务防 GC。可注入（每 app / 每测试一份）。"""

    def __init__(self) -> None:
        self.summarizing: set = set()      # 正在摘要的 npc_id
        self.tasks: set = set()            # 在途任务引用（防 GC 半路取消）

    async def _run(self, llm: Any, npc_id: str, store_dir: Optional[Path],
                   turns: Optional[List[Dict[str, Any]]] = None) -> None:
        try:
            await asyncio.to_thread(chatlog.maybe_summarize, npc_id, llm,
                                    store_dir=store_dir, turns=turns)
        except Exception as exc:                    # 后台摘要失败绝不连累回合
            log.warning("summary_background_failed", npc_id=npc_id,
                        error=short_err(exc, store_dir=store_dir))
        finally:
            self.summarizing.discard(npc_id)

    def schedule(self, llm: Any, npc_id: str, store_dir: Optional[Path],
                 turns: Optional[List[Dict[str, Any]]] = None) -> bool:
        """回合结束触发后台摘要；同 NPC 已有摘要在跑 / 没有 LLM → 跳过。返回是否启动。

        `turns` = 调用方已读好的聊天记录（省一次整文件读盘）；不传则后台自己读。
        """
        if llm is None or npc_id in self.summarizing:
            return False
        self.summarizing.add(npc_id)                # 先占位再起任务，同回合内不可重入
        task = asyncio.create_task(self._run(llm, npc_id, store_dir, turns=turns))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return True


# 兜底默认实例：直接调 stream_turn 的旧调用方（不经 server）用它
_SUMMARY_SCHEDULER = SummaryScheduler()


def _schedule_summary(llm: Any, npc_id: str, store_dir: Optional[Path]) -> bool:
    return _SUMMARY_SCHEDULER.schedule(llm, npc_id, store_dir)


# ── 记忆预取 ──────────────────────────────────────────────
# 回合开始、第一次调用 LLM 之前，把记忆条目按优先级塞进【相关记忆】块，
# 不再靠 LLM 多花一轮 recall。recall 工具保留不动，仍用于深挖。
# · 排序：pinned 在前；其余按 importance 从高到低；同级 created_at 新的在前。
# · 逐条累计 estimate_tokens 到 config.budget_memory() 为止，放不下就停
#   （整条进或整条不进，不半截截断）。
# · 不做检索、不打分、不用 entity_synonyms —— 与这句话相不相关不判断，
#   相关性深挖交给 recall 工具（memory.retrieve 语义不动）。
# · 排除 category == "action" 的条目（动作结果另有近况层负责）。
# · 没有可放的条目 → 整块省略（「（没有想起相关的事）」不进上下文）。
# · 预取只读卡，不改任何字段；失败只告警，回合照常（不带记忆块）。

def _prefetch_memory_text(npc_id: str,
                          store_dir: Optional[Path] = None,
                          entries: Optional[List[Dict[str, Any]]] = None) -> Tuple[str, int, int]:
    """按「置顶 → 重要度 → 新旧」塞进记忆预算 → (块正文, 进块条数, 被滤条数)。

    只读卡、不改任何字段。排除 action 类。无可放条目/出错 → ("", 0, …)
    （出错时被滤数无意义，记 0）。同步 IO（读记忆卡）——调用方包
    `asyncio.to_thread`。本函数不抛。
    `entries` = 调用方已读好的整张记忆卡（T7：一回合只读一次，与近况层共用）；
    不传则自己读（原签名保持兼容）。
    被滤条数 = 读到的总数 − 进块数（含 action 类和预算外的），语义同
    trace 里 memory_prefetch.filtered。
    """
    try:
        card = memory.load_card(npc_id, store_dir=store_dir) if entries is None else entries
        picked = [e for e in card if e.get("category") != "action"]
        picked.sort(key=lambda e: (not bool(e.get("pinned")),
                                   -int(e.get("importance", memory.DEFAULT_IMPORTANCE)),
                                   -float(e.get("created_at", 0.0))))
        budget = config.budget_memory()
        used = 0
        kept: List[Dict[str, Any]] = []
        for e in picked:
            line = memory.format_for_context([e])
            need = estimate_tokens(("\n" if kept else "") + line)
            if used + need > budget:
                break                      # 放不下就停：整条进或整条不进
            kept.append(e)
            used += need
        hits = len(kept)
        filtered = len(card) - hits
    except Exception as exc:
        log.warning("memory_prefetch_failed", npc_id=npc_id,
                    error=short_err(exc, store_dir=store_dir))
        return "", 0, 0
    if not kept:
        return "", hits, filtered
    return memory.format_for_context(kept), hits, filtered


# ── 近况层：最近动作结果 + 进行中 / 待确认 ──────────────────
# 【最近发生】块的三部分：
#   1. 最近动作结果：category == "action" 的记忆条目（add_action_result 写的卡），
#      最近 N 条且在最近 M 分钟内（NPC_RECENT_RESULTS=5 / NPC_RECENT_MINUTES=30）
#   2. 进行中：pending 里 mod 已受理（state=running）且未过期的 —— 「…— 进行中，已 X 秒」
#   3. 待确认：同步等待超时、至今没回报（state=unknown）—— 「…— 没收到回应，不确定是否已做成，已 X 秒」
# 两类 pending **分开写**，不能都写成"进行中"。措辞先用最朴素的中文，作者之后会改。
# 顺序：过期清理已在 /api/talk 开始时做完（server.expire_pending_actions，先写
# 「没做成: 一直没等到结果」卡再移除），这里只读 pending.alive()（未过期的）——
# 不会把已过期的条目显示成进行中。

# 最近动作结果的条数 N 与时间窗 M 分钟（默认 5 / 30）：现读可热切。
DEFAULT_RECENT_RESULTS = 5
DEFAULT_RECENT_MINUTES = 30.0
_ENV_RECENT_RESULTS = "NPC_RECENT_RESULTS"
_ENV_RECENT_MINUTES = "NPC_RECENT_MINUTES"

_PARAMS_SUMMARY_MAX = 60        # 参数摘要截断到约 60 字符


def recent_results_limit() -> int:
    """近况层「最近动作结果」的条数上限（现读，可热切）：NPC_RECENT_RESULTS，默认 5。"""
    return max(0, int(env_num(_ENV_RECENT_RESULTS, DEFAULT_RECENT_RESULTS, cast=int)))


def recent_results_minutes() -> float:
    """近况层「最近动作结果」的时间窗（现读，可热切）：NPC_RECENT_MINUTES 分钟，默认 30。"""
    return max(0.0, env_num(_ENV_RECENT_MINUTES, DEFAULT_RECENT_MINUTES))


def _params_summary(params: Any) -> str:
    """动作参数 → 摘要文本（k=v 用「，」连），截断到约 60 字符；没参数 → ""。"""
    if not isinstance(params, dict) or not params:
        return ""
    text = "，".join(f"{k}={v}" for k, v in params.items())
    return text if len(text) <= _PARAMS_SUMMARY_MAX else text[:_PARAMS_SUMMARY_MAX] + "…"


def _pending_label(action: str, params: Any) -> str:
    """「动作名(参数摘要)」；没参数就只写动作名。"""
    summary = _params_summary(params)
    return f"{action}({summary})" if summary else str(action)


def _pending_lines(infos: List[Dict[str, Any]],
                   now: Optional[float] = None) -> Tuple[List[str], List[str]]:
    """pending 条目 → (进行中行, 待确认行)，按 state **分开写**。"""
    moment = time.time() if now is None else now
    running: List[str] = []
    unknown: List[str] = []
    for info in infos:
        secs = max(0, int(moment - float(info.get("created_at", moment))))
        label = _pending_label(info.get("action", ""), info.get("params"))
        if str(info.get("state") or "unknown") == "running":
            running.append(f"{label} — 进行中，已 {secs} 秒")
        else:
            unknown.append(f"{label} — 没收到回应，不确定是否已做成，已 {secs} 秒")
    return running, unknown


def _recent_result_lines(npc_id: str, store_dir: Optional[Path] = None,
                         now: Optional[float] = None,
                         entries: Optional[List[Dict[str, Any]]] = None) -> List[str]:
    """最近 M 分钟内 category == "action" 的记忆条目，取最近 N 条（新→旧）→ 行文本。

    行文本 = 卡片正文原样（「完成: …」/「没做成: …」由 add_action_result 写卡时定）。
    同步 IO（读记忆卡）—— 调用方包 `asyncio.to_thread`。读卡失败只告警 → 空列表。
    `entries` = 调用方已读好的整张记忆卡（T7：与预取共用，一回合只读一次）；
    不传则自己读（原签名保持兼容）。
    """
    limit = recent_results_limit()
    if limit <= 0:
        return []
    moment = time.time() if now is None else now
    window = recent_results_minutes() * 60.0
    try:
        card = memory.load_card(npc_id, store_dir=store_dir) if entries is None else entries
    except Exception as exc:
        log.warning("recent_results_failed", npc_id=npc_id,
                    error=short_err(exc, store_dir=store_dir))
        return []
    action = [e for e in card if e.get("category") == "action"]
    action.sort(key=lambda e: float(e.get("created_at", 0.0)), reverse=True)
    lines: List[str] = []
    for e in action:
        if moment - float(e.get("created_at", moment)) > window:
            break                                  # 已按新→旧排，出窗后面更旧
        text = str(e.get("content") or "").strip()
        if text:
            lines.append(text)
        if len(lines) >= limit:
            break
    return lines


def _recent_layer(npc_id: str, pending_infos: List[Dict[str, Any]],
                  store_dir: Optional[Path] = None,
                  now: Optional[float] = None,
                  entries: Optional[List[Dict[str, Any]]] = None) -> Tuple[List[str], List[str], List[str]]:
    """近况层三组行 → (最近动作结果, 进行中, 待确认)。

    同步 IO（读记忆卡）—— 调用方包 `asyncio.to_thread`。本函数不抛：
    近况只是参考资料，取不到就少一块，绝不连累回合。
    `entries` = 调用方已读好的整张记忆卡（T7：与预取共用，一回合只读一次）。
    """
    moment = time.time() if now is None else now
    try:
        running, unknown = _pending_lines(pending_infos, moment)
        return _recent_result_lines(npc_id, store_dir, moment, entries), running, unknown
    except Exception as exc:                        # 防御：上面各步已各自兜底
        log.warning("recent_layer_failed", npc_id=npc_id,
                    error=short_err(exc, store_dir=store_dir))
        return [], [], []


# ── 回合 ─────────────────────────────────────────────────

# 工具定义的 token 估算缓存：键 = 能力清单的动作名（remember/recall 恒在）。
# D3：工具定义计入压缩触发的估算 —— 同一套能力整个会话反复出现，
# json.dumps + estimate_tokens 没必要每回合重算。上限 32 条，满了整体清掉重来。
_TOOL_DEFS_TOKENS: Dict[Tuple[str, ...], int] = {}
_TOOL_DEFS_TOKENS_MAX = 32


def _tool_defs_tokens(tool_defs: List[dict]) -> int:
    """工具定义数组的 token 估算（本地口径，与各层同源）。"""
    key = tuple(sorted(
        str(d.get("function", {}).get("name", "")) for d in tool_defs if isinstance(d, dict)))
    cached = _TOOL_DEFS_TOKENS.get(key)
    if cached is not None:
        return cached
    value = estimate_tokens(json.dumps(tool_defs, ensure_ascii=False))
    if len(_TOOL_DEFS_TOKENS) >= _TOOL_DEFS_TOKENS_MAX:
        _TOOL_DEFS_TOKENS.clear()
    _TOOL_DEFS_TOKENS[key] = value
    return value


async def _assemble_prompt(llm: Optional[LLMClient], npc_id: str, persona: Dict[str, Any],
                           observation: Any, message: str,
                           capabilities: Optional[List[dict]],
                           pending_actions: Optional[pending.PendingActions],
                           store_dir: Optional[Path],
                           trace: _TurnTrace) -> Tuple[List[Dict[str, Any]], List[dict], bool]:
    """上下文装配（记忆预取 / 近况层 / 历史 / 分层拼装）→ (messages, tool_defs, 回传思考?)。

    stream_turn 三段式的第一段。同步 IO 全部 `asyncio.to_thread`（本函数跑在事件循环上）；
    装配结果的 section_tokens / truncated 记进 trace。`llm is None` 在这里抛
    RuntimeError，由调用方的降级分支接住走角色卡 rules 回复。
    """
    if llm is None:
        raise RuntimeError("LLM 不可用")
    # DeepSeek 带 tools 时硬要求把 assistant 的 reasoning_content 原样回传
    # （缺了直接 400）；别的家没有这个字段，别塞给它们
    replay_reasoning = bool(getattr(llm, "replays_reasoning_content", False))
    # 记忆卡一回合只读一次（T7）：预取与近况层共用同一份，读卡挪线程池；
    # 读失败只告警（与预取失败同一条日志），两层都退化为空、回合照常
    try:
        card = await asyncio.to_thread(memory.load_card, npc_id, store_dir=store_dir)
    except Exception as exc:
        log.warning("memory_prefetch_failed", npc_id=npc_id,
                    error=short_err(exc, store_dir=store_dir))
        card = []
    # 记忆预取：置顶 → 重要度 → 新旧，到记忆预算为止，失败只告警
    memory_text, trace.mem_hits, trace.mem_filtered = await asyncio.to_thread(
        _prefetch_memory_text, npc_id, store_dir, card)
    # 近况层：最近动作结果 + 进行中 / 待确认。pending.alive() 是纯内存
    # 读（过期清理已在 /api/talk 开始时做完），最近动作结果共用上面那份卡
    pending_infos = pending_actions.alive(npc_id) if pending_actions else []
    recent_results, recent_running, recent_unknown = await asyncio.to_thread(
        _recent_layer, npc_id, pending_infos, store_dir, entries=card)
    # summarize=False：回合内不跑摘要 LLM，只读现成的摘要状态；
    # 摘要在回合结束、append_turn 之后后台触发（见 _closing_frames）
    history = await asyncio.to_thread(
        chatlog.build_history, npc_id, llm,
        token_budget=config.budget_history(store_dir=store_dir),     # 对话层预算
        store_dir=store_dir, summarize=False,
        reasoning_replay=replay_reasoning)         # DeepSeek 带 tools 要回传 CoT
    # 装配走 context.assemble（system 固定层，观测等易变块挪到
    # user 前缀并受预算约束；近况块与观测共享 state 预算）；
    # trace 的 tokens 分层直接取它的 section_tokens
    assembled = context.assemble(persona, observation, history, message,
                                 state_budget=config.budget_state(),
                                 memory_text=memory_text,
                                 memory_budget=config.budget_memory(),
                                 recent_results=recent_results,
                                 recent_running=recent_running,
                                 recent_unknown=recent_unknown)
    trace.tokens.update(assembled.section_tokens)
    # 工具定义也占上下文（D3）：计入估算，total 与 summary_trigger.prompt_tokens 自然包含它
    tool_defs = tools.build_tool_definitions(capabilities)
    trace.tokens["tools"] = _tool_defs_tokens(tool_defs)
    for cut in assembled.truncated:                # 截断绝不静默：每层一条日志
        log.warning("context_section_truncated", npc_id=npc_id, **cut)
    return assembled.messages, tool_defs, replay_reasoning


async def _run_action_call(c: Dict[str, Any], *, npc_id: str,
                           capabilities: Optional[List[dict]], execute_url: str,
                           mod: Optional[str], executor: Optional[Any],
                           execute_timeout: Callable[[], float],
                           pending_actions: Optional[pending.PendingActions],
                           store_dir: Optional[Path],
                           run: _TurnRun, trace: _TurnTrace) -> str:
    """执行一个动作工具调用 → 给 LLM 的工具消息文本。

    同步调 mod 的 /execute、写卡 / 登记 pending、action 帧记录（run.executed）都在这；
    trace 计时以进入本函数起算。
    """
    t_tool = time.perf_counter()
    outcome = await invoke_action(execute_url, npc_id, c["name"], c["args"], mod,
                                  execute_timeout(), executor, pending_actions)
    trace.add_tool(c["name"], capabilities, bool(outcome["ok"]), outcome["status"],
                   (time.perf_counter() - t_tool) * 1000)
    if outcome["status"] == "deduped":
        # 重复动作保护：同名同参的在途动作没下发给 mod ——
        # 不写卡、不登记、不进 action 帧，只回下面这条工具结果
        return action_tool_text(c["name"], outcome)
    # 记录进 action 帧：name / params 之外再带上结果（ok / status）与 call_id，
    # 否则失败的动作在帧里看起来跟做成了一样（mod 依旧不需要照它做任何事）
    run.executed.append({"name": c["name"], "params": c["args"],
                         "ok": bool(outcome["ok"]),
                         "status": outcome["status"],
                         "call_id": outcome["call_id"]})
    if outcome["status"] in ("running", "unknown"):
        # 结果还没定（mod 说在做 / 超时没等到回应）：登记 call_id 等 §4 的回报，
        # 不写卡 —— 超时不能当成"没做成"（mod 可能已经做了）。
        # state 记下"是 running 还是 unknown"：近况层据此把
        # 「进行中」和「待确认」分开写
        if pending_actions is not None:
            pending_actions.register(outcome["call_id"], npc_id, c["name"], c["args"],
                                     mod, state=outcome["status"])
    else:
        # 当场写卡（协议 §3）：成功/失败都写
        try:
            await asyncio.to_thread(memory.add_action_result, npc_id, c["name"],
                                    outcome["ok"], outcome["note"], store_dir=store_dir)
        except Exception as exc:        # 写卡失败不连累这一回合
            log.warning("action_memory_write_failed", npc_id=npc_id, action=c["name"],
                        error=short_err(exc, store_dir=store_dir))
    return action_tool_text(c["name"], outcome)


async def _run_memory_call(c: Dict[str, Any], *, npc_id: str, persona: Dict[str, Any],
                           capabilities: Optional[List[dict]], store_dir: Optional[Path],
                           trace: _TurnTrace) -> str:
    """执行一个记忆工具调用（本进程内）→ 给 LLM 的工具消息文本。"""
    t_tool = time.perf_counter()
    # 同义词族**按请求显式传入**（取自本 NPC 的角色卡）——
    # 检索没有进程级全局表，多 NPC 并发时互不污染
    result = await asyncio.to_thread(
        tools.run_tool, c["name"], c["args"], npc_id, capabilities,
        top_k=RECALL_TOP_K,
        synonyms=persona.get("entity_synonyms"),
        store_dir=store_dir)
    trace.add_tool(c["name"], capabilities, bool(result.ok), result.status.value,
                   (time.perf_counter() - t_tool) * 1000)
    return str(result.data if result.ok else (result.error or ""))


async def _stream_llm_rounds(llm: LLMClient, npc_id: str, persona: Dict[str, Any],
                             messages: List[Dict[str, Any]], tool_defs: List[dict],
                             capabilities: Optional[List[dict]],
                             execute_url: Optional[str], mod: Optional[str],
                             executor: Optional[Any],
                             execute_timeout: Callable[[], float],
                             pending_actions: Optional[pending.PendingActions],
                             store_dir: Optional[Path], replay_reasoning: bool,
                             run: _TurnRun, trace: _TurnTrace) -> AsyncGenerator[str, None]:
    """LLM ↔ 工具多轮循环（含收尾轮），逐帧产出 delta SSE 文本。

    stream_turn 三段式的第二段。工具轮预算用满后追加一次**收尾轮**（不带工具）：
    否则模型可能连续几轮只调工具，预算一耗尽就"哑巴收场"—— 动作做了，台词一句没有。

    T4（NPC_SILENT_REMEMBER 开时）每轮工具执行完先判断收尾：
      · 有非静默调用（recall / 动作）→ 照常继续（要结果的调用不能拦）；
      · 有 bad_args（模型重发才能修好）→ 继续；
      · 整个回合到现在一句台词都没出 → 继续（台词义务没完成）；
      · 否则（只有 remember 且台词已出）→ **提前收尾**（trace.ended_early），不再多跑一轮。
    另：只有静默工具且台词为空时，下一轮改 `tools=None` —— 防止模型连环 remember
    永远不开口。历史只落文本（chatlog 不存 tool_calls），提前收尾不会留下悬空调用；
    `turn_reasoning` 取收尾那一轮的思考，与台词配套。
    """
    silent_finish = silent_remember_enabled()   # T4 开关（现读，可热切）
    validate_args = validate_action_args_enabled()   # T6 开关（现读，可热切）
    force_close = False    # 只有静默工具且没台词 → 下一轮不给工具
    for round_no in range(MAX_TOOL_ROUNDS + 1):
        closing = round_no == MAX_TOOL_ROUNDS or force_close
        buffers: Dict[int, _ToolCallBuffer] = {}
        round_text: List[str] = []
        round_reasoning: List[str] = []
        trace.rounds += 1
        async for chunk in llm.stream(messages,
                                      tools=None if closing else tool_defs,
                                      reasoning_effort=thinking_effort(store_dir)):
            if chunk.has_reasoning:
                round_reasoning.append(chunk.reasoning)
            if chunk.has_content:
                round_text.append(chunk.content)
                run.full_parts.append(chunk.content)
                run.dialogue_emitted = True
                trace.note_first_delta()
                yield _sse({"type": "delta", "text": chunk.content})
            if chunk.has_tool_call:
                delta = chunk.tool_call_delta or {}
                buffers.setdefault(
                    int(delta.get("index") or 0), _ToolCallBuffer()).feed(delta)

        reasoning_text = "".join(round_reasoning)
        # 每轮覆盖：落盘/下一回合回传的是**最终答复轮**的思考（工具轮的思考
        # 只随本回合 messages 回传，不落盘）—— 收尾轮没吐思考时这里是空
        run.turn_reasoning = reasoning_text
        keys = sorted(buffers)
        calls = [buffers[k].finalize(k) for k in keys]
        bad_ids = {c["id"] for c in calls if c.get("bad_args")}
        calls = [c for c in calls if c["name"]]
        if not calls:
            break
        if closing:
            # 收尾轮没给工具，正常不会再有调用；模型硬报也只记账、不再执行（防跑飞）
            log.warning("talk_closing_round_tool_call_ignored", npc_id=npc_id)
            for c in calls:
                trace.add_tool(c["name"], capabilities, False, "ignored", 0.0)
            break

        assistant_msg = {
            "role": "assistant",
            "content": "".join(round_text),
            "tool_calls": [{
                "id": c["id"], "type": "function",
                "function": {"name": c["name"],
                             "arguments": json.dumps(c["args"], ensure_ascii=False)},
            } for c in calls],
        }
        if replay_reasoning and reasoning_text:
            # DeepSeek: 带 tools 的这一轮必须把思维链一起带回去，否则 400
            assistant_msg["reasoning_content"] = reasoning_text
        messages.append(assistant_msg)
        for c in calls:
            is_action = bool(execute_url) and tools.is_action_tool(c["name"], capabilities)
            if is_action and c["id"] in bad_ids:
                # 参数不是合法 JSON：别拿空参数去执行动作（等于拿玩家的世界瞎赌），
                # 回给模型让它重发；记忆工具照旧执行（落在自己的缺参提示上）
                tool_text = f"{c['name']} 的参数不是合法 JSON，请按工具定义重发一次"
                trace.add_tool(c["name"], capabilities, False, "bad_args", 0.0)
            elif is_action and validate_args:
                # T6：执行前按声明校验（D9）—— 非法不下发给 mod，回灌错误让模型重发
                # （trace 记 invalid_args，计入工具轮数预算）；合法则转型后照常执行
                cleaned, err = tools.validate_action_args(
                    tools.find_action(c["name"], capabilities) or {}, c["args"])
                if err:
                    log.warning("action_args_invalid", npc_id=npc_id,
                                action=c["name"], error=err)
                    tool_text = f"参数不合法：{err}，请重发"
                    trace.add_tool(c["name"], capabilities, False, "invalid_args", 0.0)
                else:
                    tool_text = await _run_action_call(
                        {**c, "args": cleaned}, npc_id=npc_id, capabilities=capabilities,
                        execute_url=execute_url, mod=mod, executor=executor,
                        execute_timeout=execute_timeout,
                        pending_actions=pending_actions, store_dir=store_dir,
                        run=run, trace=trace)
            elif is_action:
                # 动作工具：同步调 mod 的 /execute，结果当场回上下文继续推理
                tool_text = await _run_action_call(
                    c, npc_id=npc_id, capabilities=capabilities, execute_url=execute_url,
                    mod=mod, executor=executor, execute_timeout=execute_timeout,
                    pending_actions=pending_actions, store_dir=store_dir,
                    run=run, trace=trace)
            else:
                tool_text = await _run_memory_call(
                    c, npc_id=npc_id, persona=persona, capabilities=capabilities,
                    store_dir=store_dir, trace=trace)
            messages.append({
                "role": "tool", "tool_call_id": c["id"], "content": tool_text,
            })
        if round_no == MAX_TOOL_ROUNDS - 1:
            log.warning("talk_tool_rounds_exhausted", npc_id=npc_id)
        if not silent_finish:
            continue
        # T4 收尾判断：要结果的调用 / 坏参数 / 台词义务没完成 → 继续；否则提前收尾
        silent_only = all(tools.is_silent_tool(c["name"]) for c in calls)
        if not silent_only or bad_ids or not run.dialogue_emitted:
            if silent_only and not run.dialogue_emitted:
                force_close = True      # 只会 remember、一句台词没有：下一轮不给工具
            continue
        # 只有静默调用且台词已出（本轮或更早）：记住的事照常落卡，当场收尾
        trace.ended_early = True
        break


async def _closing_frames(llm: Optional[LLMClient], npc_id: str, persona: Dict[str, Any],
                          run: _TurnRun, trace: _TurnTrace, wants_voice: bool,
                          store_dir: Optional[Path],
                          scheduler: SummaryScheduler) -> AsyncGenerator[str, None]:
    """回合收尾出帧：action（已执行动作记录）→ audio?（按需）。done 帧由调用方发。

    stream_turn 三段式的第三段。摘要调度（移出关键路径）也在这一段：
    append_turn 之后才后台触发，不挡首字；失败只告警不打断收尾 —— 后面的
    action / done 帧必须发出去。
    """
    # 该不该摘要用 summary_plan（纯读）判断：本回合提示 token（trace 的
    # tokens.total，本地估算口径）≥ 窗口 × NPC_SUMMARY_RATIO 才压；
    # 拿不到估算（出错没走到装配）/ 降级回合 → 不触发，下回合再说；
    # 同 NPC 已有摘要在跑就跳过。
    # trace 的 summarized = 本回合启动了后台摘要（不是"摘要已经跑完"）。
    # plan 里的 turns 直接传给后台折叠 —— 整文件读盘只此一次，后台不再重读。
    try:
        trace_tokens_total = sum(trace.tokens.values())
        plan = None
        if not trace.llm_fallback and trace_tokens_total > 0:
            plan = await asyncio.to_thread(
                chatlog.summary_plan, npc_id, trace_tokens_total,
                store_dir=store_dir)
        trace.summarized = bool(plan) and scheduler.schedule(
            llm, npc_id, store_dir, turns=plan[0] if plan else None)
    except Exception as exc:
        log.warning("summary_trigger_failed", npc_id=npc_id,
                    error=short_err(exc, store_dir=store_dir))

    for record in run.executed:            # 已执行动作的记录（mod 不需要照它做任何事）
        yield _sse({"type": "action", "action": record})
    if wants_voice and tts.available():
        # 台词流已走完，这里才整段合成 —— 不阻塞 delta 的逐字输出
        voice = tts.voice_for(persona)
        audio = await tts.synthesize_safe("".join(run.full_parts), voice)
        if audio:
            yield _sse({"type": "audio", "audio": tts.to_base64(audio), "voice": voice})


async def stream_turn(*, llm: Optional[LLMClient], npc_id: str, message: str,
                      persona: Dict[str, Any], observation: Any = None,
                      capabilities: Optional[List[dict]] = None,
                      execute_url: Optional[str] = None, mod: Optional[str] = None,
                      executor: Optional[Any] = None, lock: asyncio.Lock,
                      pending_actions: Optional[pending.PendingActions] = None,
                      wants_voice: bool = False,
                      store_dir: Optional[Path] = None,
                      execute_timeout: Callable[[], float],
                      summary_scheduler: Optional[SummaryScheduler] = None) -> AsyncGenerator[str, None]:
    """跑完一整个回合，逐帧产出 SSE 文本（delta… / action… / audio? / done）。

    三段式（见各私有函数）：装配 `_assemble_prompt` → 工具循环 `_stream_llm_rounds`
    → 收尾出帧 `_closing_frames`；回合可变状态在 `_TurnRun`、观测记录在 `_TurnTrace`。

    - `capabilities` 为空 = 动作工具不进 LLM 的工具定义（mod 离线 / 未报到 / 没给执行接口，§1）
    - `executor` 可注入（测试用假实现）；`execute_timeout` 传的是**取值的函数**（现读，可热切）
    - `lock` = 该 NPC 的 asyncio.Lock（同 NPC 串行，防并发写覆盖）
    - `pending_actions` = 待定动作登记表（`running` 的动作等 mod 回报；不传 = 不登记）
    - `store_dir` = 记忆卡/聊天记录的运行时数据目录（None = 工程默认 store/）
    - `summary_scheduler` = 后台摘要的调度账本（server 每 app 一份；None = 模块默认实例）
    """
    scheduler = summary_scheduler if summary_scheduler is not None else _SUMMARY_SCHEDULER
    run = _TurnRun()
    trace = _TurnTrace(t_start=time.perf_counter())
    trace_sent = False                    # turn_trace 只出一条（finally 兜底防重发）
    try:
        async with lock:
            try:
                messages, tool_defs, replay_reasoning = await _assemble_prompt(
                    llm, npc_id, persona, observation, message, capabilities,
                    pending_actions, store_dir, trace)
                async for frame in _stream_llm_rounds(
                        llm, npc_id, persona, messages, tool_defs, capabilities,
                        execute_url, mod, executor, execute_timeout, pending_actions,
                        store_dir, replay_reasoning, run, trace):
                    yield frame
            except Exception as exc:                  # 降级：LLM 出错就回退角色卡的规则回复
                log.warning("talk_llm_failed", npc_id=npc_id,
                            error=short_err(exc, store_dir=store_dir, limit=160))
                if not run.dialogue_emitted:
                    fallback = rule_reply(persona, message)
                    run.full_parts.append(fallback)
                    run.dialogue_emitted = True
                    trace.llm_fallback = True
                    trace.note_first_delta()
                    yield _sse({"type": "delta", "text": fallback})
            else:
                # 正常走完：异步落盘（同步 IO 挪线程池，不卡事件循环）。
                # 落盘失败不当场断流（finally 会同步再试一次）——
                # 否则收尾的 action / audio / done 帧全都发不出去。
                try:
                    await asyncio.to_thread(chatlog.append_turn, npc_id, message,
                                            "".join(run.full_parts), store_dir=store_dir,
                                            reasoning=run.turn_reasoning)
                    run.recorded = True
                except Exception as exc:
                    log.warning("chatlog_append_failed", npc_id=npc_id,
                                error=short_err(exc, store_dir=store_dir))
            finally:
                # 兜底：异常 / 客户端断连 / 取消时也要落盘，否则这一回合的话直接丢。
                # 此时**不能 await**（生成器在 GeneratorExit / CancelledError 挂起中再 await 会报
                # "async generator ignored GeneratorExit"），故同步落盘 —— 仅异常路径，量极小。
                if not run.recorded:
                    try:
                        chatlog.append_turn(npc_id, message, "".join(run.full_parts),
                                            store_dir=store_dir, reasoning=run.turn_reasoning)
                    except Exception as exc:
                        log.warning("chatlog_append_failed", npc_id=npc_id,
                                    error=short_err(exc, store_dir=store_dir))

        async for frame in _closing_frames(llm, npc_id, persona, run, trace,
                                           wants_voice, store_dir, scheduler):
            yield frame
        # 回合收尾：一条 turn_trace（结构化日志 + 可选 JSONL）。先置位再发 —— to_thread 被
        # 取消也不至于 finally 再发一条；trace 写失败只告警（_emit_turn_trace 内吞掉），不影响回合。
        trace_sent = True
        await asyncio.to_thread(_emit_turn_trace,
                                trace.record(npc_id, message, run, store_dir=store_dir))
        yield _sse({"type": "done"})
    finally:
        if not trace_sent:
            # 断连 / 取消兜底：此时不能再 await，同步发（同上面 chatlog 落盘的理由）
            _emit_turn_trace(trace.record(npc_id, message, run, store_dir=store_dir))
