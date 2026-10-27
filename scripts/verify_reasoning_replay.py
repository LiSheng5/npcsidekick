"""DeepSeek 历史思考回传验证（V1 · T12）—— 只写脚本，由人运行。

背景：DeepSeek 官方要求「请求里带 tools 时，assistant 的 reasoning_content 必须原样回传」，
否则 400。现状（`chatlog.build_history(reasoning_replay=True)`）是**历史每一轮**都回传；
本脚本验证能不能只回传**本回合内**的思考，从而少发历史 CoT（省 prompt token、少泄露）。

做法：构造一个带简单工具的两个回合会话，直连现有 LLM 设置里的 DeepSeek 兼容端点，
把第二个回合的后续请求按三种历史各发一次：

  A 现状：历史所有 assistant 都带 reasoning_content，本回合内的工具轮也带
  B 历史轮（上一个用户回合及更早）去掉 reasoning_content，本回合内的工具轮仍带
  C 对照：连本回合内的工具轮也去掉 —— 预期 400（若也 200，说明该端点不校验）

每种记录 HTTP 状态、错误文本、`usage.prompt_tokens`；输出 `out/v1_reasoning_replay.json`，
并在控制台打印三行小表。

用法：
  python scripts/verify_reasoning_replay.py
  python scripts/verify_reasoning_replay.py --effort high --out out/v1_reasoning_replay.json

说明：
  · 配置一律取现有 LLM 设置（store/llm_config.json > 环境变量），不另造一份；
  · `--effort` 只影响本脚本发出的请求（默认 = 现有设置：没配就不发思考参数）。
    想验证"带思考"的回传行为，通常要显式给 high —— 不配思考时模型可能根本不回 CoT；
  · 没有 key / 三件套不齐 → 打印该配什么后退出（退出码 2），不发任何请求；
  · 输出里绝不打印 key（只报模型名、端点与是否就绪）。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_DEFAULT_OUT = ROOT / "out" / "v1_reasoning_replay.json"

SYSTEM_PROMPT = "你是一个测试助手。需要查时间时必须调用工具，不要凭空编。"

# 简单工具：只为让会话里出现「带 tools 的 assistant 轮」
TOOLS: List[Dict[str, Any]] = [{
    "type": "function",
    "function": {
        "name": "get_time",
        "description": "查询游戏内当前时间",
        "parameters": {"type": "object",
                       "properties": {"place": {"type": "string", "description": "地点"}},
                       "required": []},
    },
}]

# 三个变体的口径（人看表用）：(历史轮思考, 本回合内工具轮思考)
VARIANTS: Dict[str, Tuple[str, str]] = {
    "A": ("保留", "保留"),
    "B": ("去除", "保留"),
    "C": ("去除", "去除"),
}


class VerifyError(Exception):
    """准备阶段的请求失败 —— 后面的变体对比没有意义，直接中止（消息给用户看）。"""


# ── 设置（现有 LLM 设置，不另造配置）──────────────────────

def load_settings() -> Dict[str, Any]:
    """现有 LLM 三件套 + 思考档位（key 原文只在内存里用，绝不打印）。"""
    from core import llm_runtime
    from core.factory import resolve_llm_config

    cfg = llm_runtime.llm_settings()
    key = llm_runtime.effective_api_key()
    status = resolve_llm_config(api_key=key, model_name=cfg["model"], base_url=cfg["base_url"])
    return {
        "key": key,
        "model": status["model"] or "",
        "base_url": status["base_url"] or "",
        "ready": bool(status["ready"]),
        "missing": list(status["missing"]),
        "reasoning_effort": str(cfg.get("reasoning_effort") or "").strip(),
        "thinking_unsupported": bool(cfg.get("thinking_unsupported")),
    }


def endpoint_url(base_url: str) -> str:
    """OpenAI 兼容端点 → chat/completions 的完整 URL。"""
    return (base_url or "").rstrip("/") + "/chat/completions"


def thinking_body(settings: Dict[str, Any], effort: Optional[str] = None) -> Dict[str, Any]:
    """按设置（或 --effort）算思考参数 —— 借 provider 的实现，方言表只有一份。

    返回的 dict 直接并进请求体顶层（DeepSeek: thinking + reasoning_effort 平级）。
    """
    value = (effort or settings.get("reasoning_effort") or "").strip()
    if not value or settings.get("thinking_unsupported"):
        return {}
    from core.factory import create_provider

    provider = create_provider(api_key=settings["key"], model_name=settings["model"],
                               base_url=settings["base_url"])
    try:
        # 故意复用 OpenAIProvider 的方言表：脚本自己再抄一份迟早会走样
        return dict(provider._thinking_params(value))     # noqa: SLF001
    finally:
        provider.close()


# ── HTTP ────────────────────────────────────────────────

def _error_text(response: Any, data: Dict[str, Any]) -> str:
    """错误文本：优先取响应体的 error.message，其次原文前 400 字。"""
    error = data.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error)[:400]
    if isinstance(error, str):
        return error[:400]
    if data.get("message"):
        return str(data["message"])[:400]
    return (getattr(response, "text", "") or "")[:400]


def post_chat(url: str, key: str, payload: Dict[str, Any],
              timeout: float = 120.0) -> Dict[str, Any]:
    """发一次非流式 chat/completions → {status, data, error}（失败不抛）。"""
    import httpx

    from core.llm_runtime import scrub

    try:
        response = httpx.post(url, json=payload, timeout=timeout,
                              headers={"Authorization": f"Bearer {key}"})
    except Exception as exc:                     # 连不上 / 超时 / DNS
        return {"status": 0, "data": {}, "error": scrub("%s: %s" % (type(exc).__name__, exc))}
    try:
        data = response.json()
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    error = "" if response.is_success else scrub(_error_text(response, data))
    return {"status": int(response.status_code), "data": data, "error": error}


def _request(url: str, settings: Dict[str, Any], body_extra: Dict[str, Any],
             messages: List[Dict[str, Any]], tool_choice: str = "auto",
             timeout: float = 120.0) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "model": settings["model"],
        "messages": messages,
        "tools": TOOLS,
        "tool_choice": tool_choice,
        "max_tokens": 512,
        "temperature": 0.2,
    }
    payload.update(body_extra)
    return post_chat(url, settings["key"], payload, timeout=timeout)


# ── 会话与三种历史 ──────────────────────────────────────

def assistant_message(data: Dict[str, Any]) -> Dict[str, Any]:
    """响应 → 原样回传用的 assistant 消息（含 reasoning_content 与 tool_calls）。"""
    choices = data.get("choices") or []
    message = (choices[0] or {}).get("message") if choices else {}
    if not isinstance(message, dict):
        message = {}
    out: Dict[str, Any] = {"role": "assistant", "content": message.get("content") or ""}
    reasoning = message.get("reasoning_content") or message.get("reasoning")
    if isinstance(reasoning, str) and reasoning:
        out["reasoning_content"] = reasoning
    calls = message.get("tool_calls")
    if isinstance(calls, list) and calls:
        out["tool_calls"] = calls
    return out


def strip_reasoning(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """去掉 assistant 消息里的 reasoning_content（不改入参）。"""
    return [{k: v for k, v in msg.items() if k != "reasoning_content"} for msg in messages]


def build_histories(prev_turns: List[Dict[str, Any]],
                    in_turn: List[Dict[str, Any]]
                    ) -> Dict[str, Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]]:
    """三种历史 → {变体: (历史轮消息, 本回合内消息)}。

    两段必须分开（拼消息时中间还要插本回合的用户那句）：
      A 原样 / B 只去历史轮的思考 / C 连本回合工具轮也去（对照组）。
    """
    return {
        "A": (list(prev_turns), list(in_turn)),
        "B": (strip_reasoning(prev_turns), list(in_turn)),
        "C": (strip_reasoning(prev_turns), strip_reasoning(in_turn)),
    }


def _prompt_tokens(data: Dict[str, Any]) -> Optional[int]:
    usage = data.get("usage") or {}
    value = usage.get("prompt_tokens") if isinstance(usage, dict) else None
    return int(value) if isinstance(value, (int, float)) else None


def _brief(response: Dict[str, Any]) -> Dict[str, Any]:
    """一次准备请求的摘要（跑不通时人要能看出是哪一步、为什么）。"""
    message = assistant_message(response["data"])
    return {
        "status": response["status"],
        "error": response["error"],
        "reasoning_captured": bool(message.get("reasoning_content")),
        "tool_calls": len(message.get("tool_calls") or []),
        "content_len": len(message.get("content") or ""),
    }


def _tool_message(call: Dict[str, Any], content: str) -> Dict[str, Any]:
    return {"role": "tool", "tool_call_id": str(call.get("id") or "call_1"), "content": content}


def _verdict(results: Dict[str, Dict[str, Any]], reasoning_captured: bool) -> str:
    """结论文本（人看，不改配置）。"""
    status = {name: entry["status"] for name, entry in results.items()}
    if not reasoning_captured:
        return ("本次会话没有拿到 reasoning_content（多半是没开思考）—— 结果不可判定；"
                "用 --effort high 再跑一次")
    if status.get("A") == 200 and status.get("B") == 200 and status.get("C") == 400:
        return "B 通过：历史轮的思考可以不回传，只需回传本回合内的（可另开任务收窄回传范围）"
    if status.get("A") == 200 and status.get("B") == 200 and status.get("C") == 200:
        return "A/B/C 都通过：该端点不校验历史 reasoning_content（收窄与否都不影响 400）"
    if status.get("A") == 200 and status.get("B") != 200:
        return "只有现状（A）可用：历史轮的思考必须继续回传，本期不动"
    return "结论不明（见各变体状态与错误文本）"


def run(settings: Dict[str, Any], out_path: Optional[Path] = None,
        effort: Optional[str] = None, timeout: float = 120.0) -> Dict[str, Any]:
    """跑完整验证流程 → 结果字典（同时写 out_path）。"""
    url = endpoint_url(settings["base_url"])
    body_extra = thinking_body(settings, effort)

    base: List[Dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    user1 = {"role": "user", "content": "现在几点了？请调用工具查一下。"}
    # 不带 tool_choice=required：DeepSeek 的思考模式不支持它（会 400），走 auto
    first = _request(url, settings, body_extra, base + [user1], timeout=timeout)
    if first["status"] != 200:
        raise VerifyError("第一个准备请求失败（%s）：%s" % (first["status"], first["error"]))
    a1 = assistant_message(first["data"])
    setup: Dict[str, Any] = {"turn1_first": _brief(first)}

    prev_turns: List[Dict[str, Any]] = [a1]
    if a1.get("tool_calls"):
        prev_turns.append(_tool_message(a1["tool_calls"][0], "现在是 12:00"))
        second = _request(url, settings, body_extra, base + [user1] + prev_turns,
                          timeout=timeout)
        setup["turn1_second"] = _brief(second)
        if second["status"] != 200:
            raise VerifyError("第一个回合的工具结果请求失败（%s）：%s"
                              % (second["status"], second["error"]))
        prev_turns.append(assistant_message(second["data"]))

    user2 = {"role": "user", "content": "再帮我查一次，刚才那个时间的工具还能用吗？"}
    turn2 = _request(url, settings, body_extra, base + [user1] + prev_turns + [user2],
                     timeout=timeout)
    if turn2["status"] != 200:
        raise VerifyError("第二个回合的准备请求失败（%s）：%s"
                          % (turn2["status"], turn2["error"]))
    a3 = assistant_message(turn2["data"])
    setup["turn2_first"] = _brief(turn2)

    in_turn: List[Dict[str, Any]] = [a3]
    if a3.get("tool_calls"):
        in_turn.append(_tool_message(a3["tool_calls"][0], "现在是 12:05"))

    histories = build_histories(prev_turns, in_turn)
    results: Dict[str, Dict[str, Any]] = {}
    for name in VARIANTS:
        prev_part, in_turn_part = histories[name]
        messages = base + [user1] + prev_part + [user2] + in_turn_part
        response = _request(url, settings, body_extra, messages, timeout=timeout)
        results[name] = {
            "history_reasoning": VARIANTS[name][0],
            "in_turn_reasoning": VARIANTS[name][1],
            "status": response["status"],
            "error": response["error"],
            "prompt_tokens": _prompt_tokens(response["data"]),
        }

    reasoning_captured = any("reasoning_content" in msg for msg in prev_turns + in_turn)
    record: Dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "model": settings["model"],
        "base_url": settings["base_url"],
        "endpoint": url,
        "thinking_body": body_extra,
        "reasoning_captured": reasoning_captured,
        "tool_rounds": sum(1 for msg in prev_turns + in_turn if msg.get("tool_calls")),
        "setup": setup,
        "variants": results,
        "verdict": _verdict(results, reasoning_captured),
    }
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


def _print_table(record: Dict[str, Any]) -> None:
    print("")
    print("模型 %s    端点 %s    思考参数 %s"
          % (record["model"], record["endpoint"],
             json.dumps(record["thinking_body"], ensure_ascii=False) or "{}（未指定）"))
    print("%-4s %-10s %-10s %-6s %-14s %s"
          % ("变体", "历史轮思考", "本回合思考", "状态", "prompt_tokens", "错误"))
    for name, entry in record["variants"].items():
        print("%-4s %-10s %-10s %-6s %-14s %s"
              % (name, entry["history_reasoning"], entry["in_turn_reasoning"],
                 entry["status"], entry["prompt_tokens"] if entry["prompt_tokens"] is not None else "-",
                 entry["error"] or "-"))
    print("")
    print("是否拿到 reasoning_content：%s；带工具轮数：%s"
          % ("是" if record["reasoning_captured"] else "否", record["tool_rounds"]))
    for name, info in (record.get("setup") or {}).items():
        print("准备 %-13s 状态 %-4s 思考 %-2s 工具调用 %s %s"
              % (name, info["status"], "有" if info["reasoning_captured"] else "无",
                 info["tool_calls"], ("| " + info["error"]) if info["error"] else ""))
    print("结论：%s" % record["verdict"])


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="DeepSeek 历史思考回传验证（V1）")
    parser.add_argument("--out", default=str(_DEFAULT_OUT), help="结果 JSON 路径")
    parser.add_argument("--effort", default="",
                        help="只影响本脚本的思考档位（默认 = 现有设置；通常给 high）")
    parser.add_argument("--timeout", type=float, default=120.0, help="单次请求超时（秒）")
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except Exception:
                pass

    settings = load_settings()
    if not settings["ready"]:
        print("未配置完整，无法验证（缺：%s）。" % "、".join(settings["missing"]))
        print("请先配好 NPC_API_KEY / AGENT_MODEL / NPC_BASE_URL（或控制台「模型」页），再跑本脚本。")
        return 2

    try:
        record = run(settings, Path(args.out), effort=args.effort or None, timeout=args.timeout)
    except VerifyError as exc:
        print("验证中止：%s" % exc)
        return 1
    _print_table(record)
    print("已写 %s" % args.out)
    return 0 if record["variants"]["A"]["status"] == 200 else 1


if __name__ == "__main__":
    raise SystemExit(main())
