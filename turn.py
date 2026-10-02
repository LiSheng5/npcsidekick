"""回合主体 —— 一次 /api/talk 的 LLM ↔ 工具多轮循环（产出 SSE 帧）。

执行模型（2026-09-28 起）：**动作 = LLM 直接调用的工具**。
  记忆工具由本进程执行；动作工具由本模块**同步调用** mod 的 `execute_url`（协议 §3），
  结果作为工具消息回上下文继续推理，并**当场写进记忆卡** —— mod 不用回报。

两条通道严格分离（台词只走 delta 帧，流末尾的 action 帧只是"已执行动作"的记录）：
  · 台词通道 = SSE 的 delta 帧，只有角色说的话
  · 结构化通道 = action 帧，在流末尾，mod **不需要**照它做任何事（仅记录/展示）
  · 语音通道 = audio 帧（仅 voice=true 时），在 done 之前，缺依赖/超时则不出帧

同步文件 IO（记忆修剪 / 聊天记录读写）一律 `asyncio.to_thread` ——
本函数是 SSE 生成器，跑在事件循环上，直接调用会把整个服务器卡住。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional

from core.client import LLMClient
from core.llm_runtime import scrub, thinking_effort
from core.logging_config import log
from core.personas import build_system_prompt, rule_reply

import chatlog
import memory
import tools
import tts

# 单次对话内的工具调用轮次上限（记忆 + 动作共用这个预算，防跑飞）
MAX_TOOL_ROUNDS = 3

# 记忆检索返回条数（recall 工具）
RECALL_TOP_K = 5


def _sse(payload: Dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


# ── 流式工具调用累积 ─────────────────────────────────────

class _ToolCallBuffer:
    """把跨 chunk 的 tool_call 增量拼成完整调用。"""

    def __init__(self) -> None:
        self.name = ""
        self.arguments = ""
        self.call_id = ""

    def feed(self, delta: Dict[str, Any]) -> None:
        if delta.get("id"):
            self.call_id = str(delta["id"])
        if delta.get("function_name"):
            self.name += str(delta["function_name"])
        if delta.get("function_arguments"):
            self.arguments += str(delta["function_arguments"])

    def finalize(self, index: int) -> Dict[str, Any]:
        try:
            args = json.loads(self.arguments) if self.arguments.strip() else {}
        except json.JSONDecodeError:
            log.warning("tool_args_bad_json", tool=self.name, raw=self.arguments[:120])
            args = {}
        if not isinstance(args, dict):
            args = {}
        return {"id": self.call_id or f"call_{index + 1}", "name": self.name, "args": args}


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
    """同步请求 mod 执行一个动作，返回统一形态 `{ok, status, note}`。

    - 超时 / 连不上 / 响应非法 → `ok=False` 的失败，**绝不假装做成**（协议 §3）
    - `status`：`done`（已做完，默认）/ `running`（mod 说还在做）
    - `executor` 可注入（测试用假实现）；缺省走真实 HTTP（`_http_execute`）
    """
    payload = {"npc_id": npc_id, "action": name, "params": args, "mod": mod}
    runner = executor or _http_execute
    try:
        data = await asyncio.wait_for(runner(execute_url, payload, timeout), timeout=timeout)
    except asyncio.TimeoutError:
        log.warning("action_execute_timeout", npc_id=npc_id, action=name, timeout_s=timeout)
        return {"ok": False, "status": "failed", "note": f"等待超过 {timeout:g}s 仍未返回"}
    except Exception as exc:
        log.warning("action_execute_failed", npc_id=npc_id, action=name,
                    error=type(exc).__name__)
        return {"ok": False, "status": "failed",
                "note": f"执行接口没接上（{type(exc).__name__}）"}
    if not isinstance(data, dict):
        return {"ok": False, "status": "failed", "note": "执行接口回了非法响应"}
    status = str(data.get("status") or "done").strip().lower()
    if status not in ("done", "running"):
        status = "done"
    return {"ok": bool(data.get("ok")), "status": status,
            "note": str(data.get("note") or "").strip()}


def action_tool_text(name: str, outcome: Dict[str, Any]) -> str:
    """动作结果 → 给 LLM 的工具消息文本（进上下文的，不是写卡文案）。"""
    note = str(outcome.get("note") or "").strip()
    if outcome.get("status") == "running":
        return f"{name} 还在进行中，结果稍后才出来"
    if outcome.get("ok"):
        return note or f"{name} 已完成"
    return f"没做成: {note or name}"


# ── 回合 ─────────────────────────────────────────────────

async def stream_turn(*, llm: Optional[LLMClient], npc_id: str, message: str,
                      persona: Dict[str, Any], observation: Any = None,
                      capabilities: Optional[List[dict]] = None,
                      execute_url: Optional[str] = None, mod: Optional[str] = None,
                      executor: Optional[Any] = None, lock: asyncio.Lock,
                      wants_voice: bool = False,
                      execute_timeout: Callable[[], float]) -> AsyncGenerator[str, None]:
    """跑完一整个回合，逐帧产出 SSE 文本（delta… / action… / audio? / done）。

    - `capabilities` 为空 = 动作工具不进 LLM 的工具定义（mod 离线 / 未报到 / 没给执行接口，§1）
    - `executor` 可注入（测试用假实现）；`execute_timeout` 传的是**取值的函数**（现读，可热切）
    - `lock` = 该 NPC 的 asyncio.Lock（同 NPC 串行，防并发写覆盖）
    """
    full_parts: List[str] = []
    executed: List[Dict[str, Any]] = []     # 本回合已执行的动作（流末尾出 action 帧）
    emitted = False
    async with lock:
        try:
            if llm is None:
                raise RuntimeError("LLM 不可用")
            # 同步阻塞调用一律挪到线程池：本函数是 SSE 生成器，跑在事件循环上，
            # 直接调用会把整个服务器卡住 —— 尤其 build_history 内那次同步摘要 LLM 请求。
            await asyncio.to_thread(memory.prune, npc_id)   # 确定性遗忘（半衰期修剪，不走 LLM）
            history = await asyncio.to_thread(chatlog.build_history, npc_id, llm)
            messages: List[Dict[str, Any]] = [
                {"role": "system", "content": build_system_prompt(persona, observation)}]
            messages.extend(history)
            messages.append({"role": "user", "content": message})
            tool_defs = tools.build_tool_definitions(capabilities)

            for round_no in range(MAX_TOOL_ROUNDS):
                buffers: Dict[int, _ToolCallBuffer] = {}
                round_text: List[str] = []
                async for chunk in llm.stream(messages, tools=tool_defs,
                                              reasoning_effort=thinking_effort()):
                    if chunk.has_content:
                        round_text.append(chunk.content)
                        full_parts.append(chunk.content)
                        emitted = True
                        yield _sse({"type": "delta", "text": chunk.content})
                    if chunk.has_tool_call:
                        delta = chunk.tool_call_delta or {}
                        buffers.setdefault(
                            int(delta.get("index") or 0), _ToolCallBuffer()).feed(delta)

                calls = [buffers[k].finalize(k) for k in sorted(buffers)]
                calls = [c for c in calls if c["name"]]
                if not calls:
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
                    if execute_url and tools.is_action_tool(c["name"], capabilities):
                        # 动作工具：同步调 mod 的 /execute，结果当场回上下文继续推理
                        outcome = await invoke_action(execute_url, npc_id, c["name"],
                                                      c["args"], mod, execute_timeout(),
                                                      executor)
                        executed.append({"name": c["name"], "params": c["args"]})
                        tool_text = action_tool_text(c["name"], outcome)
                        if outcome["status"] != "running":
                            # 当场写卡（协议 §3）：成功/失败都写；running 结果还没定，
                            # 不写（等 §4 的异步回报）
                            try:
                                await asyncio.to_thread(
                                    memory.add_action_result, npc_id, c["name"],
                                    outcome["ok"], outcome["note"])
                            except Exception as exc:    # 写卡失败不连累这一回合
                                log.warning("action_memory_write_failed", npc_id=npc_id,
                                            action=c["name"], error=scrub(str(exc))[:120])
                    else:
                        # 同义词族**按请求显式传入**（取自本 NPC 的角色卡）——
                        # 不走 memory 的进程级全局表，多 NPC 并发时互不污染
                        result = await asyncio.to_thread(
                            tools.run_tool, c["name"], c["args"], npc_id, capabilities,
                            top_k=RECALL_TOP_K,
                            synonyms=persona.get("entity_synonyms"))
                        tool_text = str(result.data if result.ok else (result.error or ""))
                    messages.append({
                        "role": "tool", "tool_call_id": c["id"], "content": tool_text,
                    })
                if round_no == MAX_TOOL_ROUNDS - 1:
                    log.warning("talk_tool_rounds_exhausted", npc_id=npc_id)
        except Exception as exc:                            # 降级：LLM 出错就回退角色卡的规则回复
            log.warning("talk_llm_failed", npc_id=npc_id, error=scrub(str(exc))[:160])
            if not emitted:
                fallback = rule_reply(persona, message)
                full_parts.append(fallback)
                emitted = True
                yield _sse({"type": "delta", "text": fallback})

        await asyncio.to_thread(chatlog.append_turn, npc_id, message,
                                "".join(full_parts))

    for record in executed:                    # 已执行动作的记录（mod 不需要照它做任何事）
        yield _sse({"type": "action", "action": record})
    if wants_voice and tts.available():
        # 台词流已走完，这里才整段合成 —— 不阻塞 delta 的逐字输出
        voice = tts.voice_for(persona)
        audio = await tts.synthesize_safe("".join(full_parts), voice)
        if audio:
            yield _sse({"type": "audio", "audio": tts.to_base64(audio), "voice": voice})
    yield _sse({"type": "done"})
