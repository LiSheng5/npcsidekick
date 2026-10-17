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

同步文件 IO（记忆修剪 / 聊天记录读写）一律 `asyncio.to_thread` ——
本函数是 SSE 生成器，跑在事件循环上，直接调用会把整个服务器卡住。
唯一例外：客户端断连 / 取消时的落盘兜底（文末 `finally`）—— 那种挂起状态下不能再 await。
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional, Tuple

from core.client import LLMClient
from core.config_flags import env_num, env_text
from core.llm_runtime import scrub, thinking_effort
from core.logging_config import log
from core.personas import rule_reply

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


def _sse(payload: Dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


# ── 回合 trace（任务 1）────────────────────────────────────
# 每回合结束输出一条 `turn_trace` 结构化日志（含降级回合）：
# 记什么、不记什么见各字段说明 —— 默认**只记长度不记原文**（隐私 + 日志体积）。
# NPC_TRACE_FILE 非空时同一条记录再以 JSONL 追加写入该文件；写失败只告警，不影响回合。

_ENV_TRACE_FILE = "NPC_TRACE_FILE"


def _emit_turn_trace(record: Dict[str, Any]) -> None:
    """输出一条 turn_trace：结构化日志 + 可选 JSONL 落盘。全程不抛、不影响回合。"""
    try:
        log.info("turn_trace", **record)
    except Exception as exc:                       # 日志坏了也不能连累回合
        log.warning("turn_trace_log_failed", error=scrub(str(exc))[:120])
    path = env_text(_ENV_TRACE_FILE, "").strip()
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:
        log.warning("turn_trace_write_failed", path=path, error=scrub(str(exc))[:120])


def _trace_tool(name: str, capabilities: Optional[List[dict]], ok: bool,
                status: str, ms: float) -> Dict[str, Any]:
    """trace 的一条工具记录。`status` 如实保留执行侧取值（done/running/unknown/failed、
    memory 的 success/error/rejected，以及 bad_args / ignored / 之后的 deduped）。"""
    return {
        "name": name,
        "kind": "action" if tools.is_action_tool(name, capabilities) else "memory",
        "ok": bool(ok),
        "status": status,
        "ms": round(ms, 1),
    }


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
                        executor: Optional[Any] = None) -> Dict[str, Any]:
    """同步请求 mod 执行一个动作，返回统一形态 `{ok, status, note, call_id}`。

    - 连不上 / 响应非法 → `ok=False` 的失败（请求大概率没到 mod），**绝不假装做成**（协议 §3）
    - **超时** → `status="unknown"`：请求可能已经到 mod 并被执行了，只是没等到回应，
      故不当作失败，由调用方登记待定（`pending`）等回报，TTL 到点再算账
    - `status`：`done`（已做完，默认）/ `running`（mod 说还在做）/ `unknown`（超时未定）
      / `failed`（大脑侧失败 —— 只给超时以外那些确定没做成的）
    - `call_id`：本次调用的关联 id（`uuid4().hex`，随请求发给 mod，Mod 回报时原样带回）——
      不复用 LLM 的 tool_call id，那个不保证唯一
    - `executor` 可注入（测试用假实现）；缺省走真实 HTTP（`_http_execute`）
    """
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
    status = str(data.get("status") or "done").strip().lower()
    if status not in ("done", "running"):
        status = "done"
    return {"ok": bool(data.get("ok")), "status": status, "call_id": call_id,
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
    if outcome.get("ok"):
        return note or f"{name} 已完成"
    return f"没做成: {note or name}"


class _SummarizeProbe:
    """把 LLM 包一层，只数 `build_history` 期间摘要用的 `chat` 调用（trace 的 `summarized`）。

    `build_history` 内部自己调 `maybe_summarize` 且不向外返回结果；
    包一层计数是不动 chatlog 签名的最小做法。`chat_calls > 0` ⇔ 本回合触发了摘要。
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.chat_calls = 0

    def chat(self, *args: Any, **kwargs: Any) -> Any:
        self.chat_calls += 1
        return self._inner.chat(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


# ── 记忆预取（任务 4，简化方案）────────────────────────
# 回合开始、第一次调用 LLM 之前，把「pinned 条目 + 最近 N 条」放进【相关记忆】块，
# 不再靠 LLM 多花一轮 recall。recall 工具保留不动，仍用于深挖。
# · 不做检索、不打分、不用 entity_synonyms —— 与这句话相不相关不判断，
#   相关性深挖交给 recall 工具（memory.retrieve 语义不动）。
# · 排除 category == "action" 的条目（动作结果另有任务 5 的近况层负责）。
# · 没有可放的条目 → 整块省略（「（没有想起相关的事）」不进上下文）。
# · 块内排序：pinned 在前、其余按 created_at 从新到旧 —— 预算截断从尾部砍，
#   等于 pinned 最后才被砍（"截断时 pinned 优先"）。
# 预取失败只告警，回合照常（不带记忆块）。

# 「最近 N 条」的 N（**需确认**默认 5）：现读可热切。
DEFAULT_PREFETCH_RECENT = 5
_ENV_PREFETCH_RECENT = "NPC_PREFETCH_RECENT"


def prefetch_recent() -> int:
    """记忆预取的「最近 N 条」（现读，可热切）：NPC_PREFETCH_RECENT，默认 5。"""
    return max(0, int(env_num(_ENV_PREFETCH_RECENT, DEFAULT_PREFETCH_RECENT, cast=int)))


def _prefetch_memory_text(npc_id: str,
                          store_dir: Optional[Path] = None) -> Tuple[str, int, int]:
    """pinned 条目 + 最近 N 条（都排除 action 类）→ (块正文, 进块条数, 被滤条数)。

    无可放条目/出错 → ("", 0, …)（出错时被滤数无意义，记 0）。同步 IO（读记忆卡）——
    调用方包 `asyncio.to_thread`。本函数不抛。
    被滤条数 = 读到但没进块的条数（action 类 + 排不进最近 N 的）。pinned 不占 N 的名额。
    """
    try:
        card = memory.load_card(npc_id, store_dir=store_dir)
        entries = [e for e in card if e.get("category") != "action"]
        entries.sort(key=lambda e: float(e.get("created_at", 0.0)), reverse=True)
        pinned = [e for e in entries if e.get("pinned")]
        recent = [e for e in entries if not e.get("pinned")][:prefetch_recent()]
        kept = pinned + recent        # pinned 在前，预算截断先砍尾部（新→旧）
        hits = len(kept)
        filtered = len(card) - hits
    except Exception as exc:
        log.warning("memory_prefetch_failed", npc_id=npc_id,
                    error=scrub(str(exc))[:120])
        return "", 0, 0
    if not kept:
        return "", hits, filtered
    return memory.format_for_context(kept), hits, filtered


# ── 回合 ─────────────────────────────────────────────────

async def stream_turn(*, llm: Optional[LLMClient], npc_id: str, message: str,
                      persona: Dict[str, Any], observation: Any = None,
                      capabilities: Optional[List[dict]] = None,
                      execute_url: Optional[str] = None, mod: Optional[str] = None,
                      executor: Optional[Any] = None, lock: asyncio.Lock,
                      pending_actions: Optional[pending.PendingActions] = None,
                      wants_voice: bool = False,
                      store_dir: Optional[Path] = None,
                      execute_timeout: Callable[[], float]) -> AsyncGenerator[str, None]:
    """跑完一整个回合，逐帧产出 SSE 文本（delta… / action… / audio? / done）。

    - `capabilities` 为空 = 动作工具不进 LLM 的工具定义（mod 离线 / 未报到 / 没给执行接口，§1）
    - `executor` 可注入（测试用假实现）；`execute_timeout` 传的是**取值的函数**（现读，可热切）
    - `lock` = 该 NPC 的 asyncio.Lock（同 NPC 串行，防并发写覆盖）
    - `pending_actions` = 待定动作登记表（`running` 的动作等 mod 回报；不传 = 不登记）
    - `store_dir` = 记忆卡/聊天记录的运行时数据目录（None = 工程默认 store/）
    """
    t_start = time.perf_counter()           # 回合开始（含等锁）—— trace 的计时基准
    full_parts: List[str] = []
    executed: List[Dict[str, Any]] = []     # 本回合已执行的动作（流末尾出 action 帧）
    emitted = False
    recorded = False                        # 聊天记录是否已落盘（finally 兜底用）
    # 回合 trace（任务 1）：只记状态/长度/耗时，不记玩家原文与台词原文（隐私 + 日志体积）
    trace_rounds = 0                        # 实际发起的 LLM 轮数
    trace_tools: List[Dict[str, Any]] = []  # 每次工具调用 {name, kind, ok, status, ms}
    trace_llm_fallback = False              # 是否走了规则回复
    trace_summarized = False                # 本回合是否触发了摘要
    trace_tokens: Dict[str, int] = {"system": 0, "history": 0, "state": 0,
                                    "memory": 0, "user": 0}
    trace_mem_hits = 0                      # 记忆预取进块条数（含 pinned）
    trace_mem_filtered = 0                  # 记忆预取被相关性阈值滤掉的条数
    trace_first_delta_at: Optional[float] = None
    trace_emitted = False                   # turn_trace 只出一条（finally 兜底防重发）

    def _trace_record() -> Dict[str, Any]:
        """拼 turn_trace 记录（发出时现算耗时；tokens.total = 各层之和）。"""
        return {
            "npc_id": npc_id,
            "rounds": trace_rounds,
            "llm_fallback": trace_llm_fallback,
            "summarized": trace_summarized,
            "no_dialogue": not emitted,
            "tools": trace_tools,
            "tokens": {**trace_tokens, "total": sum(trace_tokens.values())},
            "memory_prefetch": {"hits": trace_mem_hits, "filtered": trace_mem_filtered},
            "first_token_ms": (None if trace_first_delta_at is None
                               else round((trace_first_delta_at - t_start) * 1000, 1)),
            "total_ms": round((time.perf_counter() - t_start) * 1000, 1),
            "message_len": len(message),
            "reply_len": len("".join(full_parts)),
        }

    try:
        async with lock:
            try:
                if llm is None:
                    raise RuntimeError("LLM 不可用")
                # 同步阻塞调用一律挪到线程池：本函数是 SSE 生成器，跑在事件循环上，
                # 直接调用会把整个服务器卡住 —— 尤其 build_history 内那次同步摘要 LLM 请求。
                await asyncio.to_thread(memory.prune, npc_id,
                                        store_dir=store_dir)   # 确定性遗忘（半衰期修剪，不走 LLM）
                # 包一层只为数摘要调用（trace 的 summarized），不改 build_history 行为
                probe = _SummarizeProbe(llm)
                # 记忆预取（任务 4，简化方案）：pinned + 最近 N 条，失败只告警
                memory_text, trace_mem_hits, trace_mem_filtered = await asyncio.to_thread(
                    _prefetch_memory_text, npc_id, store_dir)
                history = await asyncio.to_thread(
                    chatlog.build_history, npc_id, probe,
                    token_budget=context.budget_history(),   # 对话层预算（任务 3）
                    store_dir=store_dir)
                trace_summarized = probe.chat_calls > 0
                # 装配走 context.assemble（任务 3：system 固定层，观测等易变块挪到
                # user 前缀并受预算约束）；trace 的 tokens 分层直接取它的 section_tokens
                assembled = context.assemble(persona, observation, history, message,
                                             state_budget=context.budget_state(),
                                             memory_text=memory_text,
                                             memory_budget=context.budget_memory())
                messages = assembled.messages
                trace_tokens.update(assembled.section_tokens)
                for cut in assembled.truncated:            # 截断绝不静默：每层一条日志
                    log.warning("context_section_truncated", npc_id=npc_id, **cut)
                tool_defs = tools.build_tool_definitions(capabilities)

                # 工具轮预算用满后追加一次**收尾轮**（不带工具）：否则模型可能连续几轮只调工具，
                # 预算一耗尽就"哑巴收场"—— 动作做了，台词一句没有。
                for round_no in range(MAX_TOOL_ROUNDS + 1):
                    closing = round_no == MAX_TOOL_ROUNDS
                    buffers: Dict[int, _ToolCallBuffer] = {}
                    round_text: List[str] = []
                    trace_rounds += 1
                    async for chunk in llm.stream(messages,
                                                  tools=None if closing else tool_defs,
                                                  reasoning_effort=thinking_effort(store_dir)):
                        if chunk.has_content:
                            round_text.append(chunk.content)
                            full_parts.append(chunk.content)
                            emitted = True
                            if trace_first_delta_at is None:
                                trace_first_delta_at = time.perf_counter()
                            yield _sse({"type": "delta", "text": chunk.content})
                        if chunk.has_tool_call:
                            delta = chunk.tool_call_delta or {}
                            buffers.setdefault(
                                int(delta.get("index") or 0), _ToolCallBuffer()).feed(delta)

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
                            trace_tools.append(_trace_tool(c["name"], capabilities, False,
                                                          "ignored", 0.0))
                        break

                    messages.append({
                        "role": "assistant",
                        "content": "".join(round_text),
                        "tool_calls": [{
                            "id": c["id"], "type": "function",
                            "function": {"name": c["name"],
                                         "arguments": json.dumps(c["args"], ensure_ascii=False)},
                        } for c in calls],
                    })
                    for c in calls:
                        t_tool = time.perf_counter()
                        is_action = bool(execute_url) and tools.is_action_tool(c["name"], capabilities)
                        if is_action and c["id"] in bad_ids:
                            # 参数不是合法 JSON：别拿空参数去执行动作（等于拿玩家的世界瞎赌），
                            # 回给模型让它重发；记忆工具照旧执行（落在自己的缺参提示上）
                            tool_text = f"{c['name']} 的参数不是合法 JSON，请按工具定义重发一次"
                            trace_tools.append(_trace_tool(c["name"], capabilities, False,
                                                          "bad_args", 0.0))
                        elif is_action:
                            # 动作工具：同步调 mod 的 /execute，结果当场回上下文继续推理
                            outcome = await invoke_action(execute_url, npc_id, c["name"],
                                                          c["args"], mod, execute_timeout(),
                                                          executor)
                            trace_tools.append(_trace_tool(
                                c["name"], capabilities, bool(outcome["ok"]),
                                outcome["status"], (time.perf_counter() - t_tool) * 1000))
                            # 记录进 action 帧：name / params 之外再带上结果（ok / status）与 call_id，
                            # 否则失败的动作在帧里看起来跟做成了一样（mod 依旧不需要照它做任何事）
                            executed.append({"name": c["name"], "params": c["args"],
                                             "ok": bool(outcome["ok"]),
                                             "status": outcome["status"],
                                             "call_id": outcome["call_id"]})
                            tool_text = action_tool_text(c["name"], outcome)
                            if outcome["status"] in ("running", "unknown"):
                                # 结果还没定（mod 说在做 / 超时没等到回应）：登记 call_id 等 §4 的回报，
                                # 不写卡 —— 超时不能当成"没做成"（mod 可能已经做了）
                                if pending_actions is not None:
                                    pending_actions.register(outcome["call_id"], npc_id, c["name"],
                                                             c["args"], mod)
                            else:
                                # 当场写卡（协议 §3）：成功/失败都写
                                try:
                                    await asyncio.to_thread(
                                        memory.add_action_result, npc_id, c["name"],
                                        outcome["ok"], outcome["note"], store_dir=store_dir)
                                except Exception as exc:    # 写卡失败不连累这一回合
                                    log.warning("action_memory_write_failed", npc_id=npc_id,
                                                action=c["name"], error=scrub(str(exc))[:120])
                        else:
                            # 同义词族**按请求显式传入**（取自本 NPC 的角色卡）——
                            # 检索没有进程级全局表，多 NPC 并发时互不污染
                            result = await asyncio.to_thread(
                                tools.run_tool, c["name"], c["args"], npc_id, capabilities,
                                top_k=RECALL_TOP_K,
                                synonyms=persona.get("entity_synonyms"),
                                store_dir=store_dir)
                            trace_tools.append(_trace_tool(
                                c["name"], capabilities, bool(result.ok),
                                result.status.value, (time.perf_counter() - t_tool) * 1000))
                            tool_text = str(result.data if result.ok else (result.error or ""))
                        messages.append({
                            "role": "tool", "tool_call_id": c["id"], "content": tool_text,
                        })
                    if round_no == MAX_TOOL_ROUNDS - 1:
                        log.warning("talk_tool_rounds_exhausted", npc_id=npc_id)
            except Exception as exc:                        # 降级：LLM 出错就回退角色卡的规则回复
                log.warning("talk_llm_failed", npc_id=npc_id, error=scrub(str(exc))[:160])
                if not emitted:
                    fallback = rule_reply(persona, message)
                    full_parts.append(fallback)
                    emitted = True
                    trace_llm_fallback = True
                    if trace_first_delta_at is None:
                        trace_first_delta_at = time.perf_counter()
                    yield _sse({"type": "delta", "text": fallback})
            else:
                # 正常走完：异步落盘（同步 IO 挪线程池，不卡事件循环）
                await asyncio.to_thread(chatlog.append_turn, npc_id, message,
                                        "".join(full_parts), store_dir=store_dir)
                recorded = True
            finally:
                # 兜底：异常 / 客户端断连 / 取消时也要落盘，否则这一回合的话直接丢。
                # 此时**不能 await**（生成器在 GeneratorExit / CancelledError 挂起中再 await 会报
                # "async generator ignored GeneratorExit"），故同步落盘 —— 仅异常路径，量极小。
                if not recorded:
                    try:
                        chatlog.append_turn(npc_id, message, "".join(full_parts),
                                            store_dir=store_dir)
                    except Exception as exc:
                        log.warning("chatlog_append_failed", npc_id=npc_id,
                                    error=scrub(str(exc))[:120])

        for record in executed:                # 已执行动作的记录（mod 不需要照它做任何事）
            yield _sse({"type": "action", "action": record})
        if wants_voice and tts.available():
            # 台词流已走完，这里才整段合成 —— 不阻塞 delta 的逐字输出
            voice = tts.voice_for(persona)
            audio = await tts.synthesize_safe("".join(full_parts), voice)
            if audio:
                yield _sse({"type": "audio", "audio": tts.to_base64(audio), "voice": voice})
        # 回合收尾：一条 turn_trace（结构化日志 + 可选 JSONL）。先置位再发 —— to_thread 被
        # 取消也不至于 finally 再发一条；trace 写失败只告警（_emit_turn_trace 内吞掉），不影响回合。
        trace_emitted = True
        await asyncio.to_thread(_emit_turn_trace, _trace_record())
        yield _sse({"type": "done"})
    finally:
        if not trace_emitted:
            # 断连 / 取消兜底：此时不能再 await，同步发（同上面 chatlog 落盘的理由）
            _emit_turn_trace(_trace_record())
