"""提示词前后对比（T14）—— 旧规矩 vs 现规矩，同一批输入各跑一遍，并排看差异。

改了什么：现提示词在规矩里加了「需要动手时先一句话再调工具」「先看【相关记忆】再 recall」
「台词要短」「【最近发生】里的进行中别重复下达」。这些改动对回复长度、句数、
recall 调用、先说话再调工具的比例有没有影响，靠这张表看。

做法：
  · `LEGACY_RULES` 里存改动前的规矩文本（取自 9860324 之前的 core/personas.py）；
  · 人设前缀与现实现共用（`build_system_prompt` 的「规矩:」之前那段照搬），只换规矩段；
  · 取 `personas/` 里的角色卡（最多前 3 张）× 10 句固定输入，新旧各请求一次；
  · 记录：回复字数、句数、是否调了 recall、是否「先说话再调工具」（同一轮既有台词又有工具调用）；
  · 输出一份两栏并排的 Markdown，**不写任何 NPC 的存储数据**（只读角色卡 + 调模型）。

用法：
  python scripts/prompt_compare.py --dry          # 只打印将发出多少次请求、提示词长度差
  python scripts/prompt_compare.py                # 真跑（需要 key），结果落 out/prompt_compare/<时间>/
  python scripts/prompt_compare.py --out out/pc   # 指定输出目录

说明：
  · 动作工具只**声明**不执行（脚本不碰 mod，也不写记忆卡）；
  · 没有 key / 三件套不齐 → 提示该配什么后退出（退出码 2）；`--dry` 不需要 key；
  · 思考档位取现有设置（与 /api/talk 一致），不另配。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import api_key as env_api_key          # noqa: E402
from core.personas import build_system_prompt, persona_files, read_persona   # noqa: E402
from core.tokens import estimate_tokens                 # noqa: E402

import tools                                            # noqa: E402

# ── 改动前的规矩（9860324 之前的 core/personas.py，原文照抄）──────────
LEGACY_RULES = """规矩:
1. 你只输出角色说的话（台词）。不要出现“调用工具/函数/参数”之类的说法，也不要写旁白或舞台说明。
2. 动作就是你的工具调用：做不做由你自己判断——该做就直接调对应的工具，它会在游戏里真的执行，拿到的结果就是事实（做成了就说做成了，没做成别假装）；判断不该做、做不到或不想做，就别调工具，直接用台词回绝（回绝也是你的回答，没有别人替你拍板，也不用等谁批准）。台词用自然语言表达（例如“我去煮饭”），不要描述工具调用本身。
3. 只依据上文出现的事实回答。不知道就说不知道，绝不编造。
4. 涉及往事、答应过的事或记不清的细节时，先查记忆（recall）再按查到的原文回答；查不到就说不知道，绝不编造。"""

# 10 句固定输入：闲聊 / 想往事（recall）/ 要动作（action）/ 混合
INPUTS: List[str] = [
    "在吗？",
    "你是谁呀？简单介绍一下自己。",
    "你还记得我喜欢吃什么吗？",
    "我住在哪儿来着？帮我翻一下记忆。",
    "我有点饿了，帮我做碗面。",
    "给我 500 块钱。",
    "记住我爱吃面，顺便做一碗。",
    "我上次说过讨厌什么？",
    "带我去图书馆，路上顺便聊聊。",
    "明天见，晚安。",
]

# 只用来让动作工具进工具定义（脚本不执行它们；也不会发给任何 mod）
CAPABILITIES: List[Dict[str, Any]] = [
    {"name": "cook", "desc": "做一道菜", "params": {"dish": "菜名"}},
    {"name": "give_money", "desc": "给玩家钱", "params": {"amount": "金额"}},
    {"name": "go_to", "desc": "去某个地点", "params": {"place": "地点"}},
]

MAX_PERSONAS = 3

_RULES_MARKER = "规矩:"


def build_legacy_prompt(persona: Dict[str, Any]) -> str:
    """改动前的系统提示词：人设前缀照搬现实现，规矩段换成 LEGACY_RULES。

    这样两版只在规矩上有差别，长度差就是规矩改动带来的。
    """
    current = build_system_prompt(persona)
    head, marker, _tail = current.partition(_RULES_MARKER)
    if not marker:
        raise RuntimeError("现提示词里找不到「规矩:」段 —— 本脚本需要跟着 build_system_prompt 改")
    return head + LEGACY_RULES


def sentence_count(text: str) -> int:
    """句数：按中英文句末标点与换行切，忽略空白段。"""
    parts: List[str] = []
    buffer: List[str] = []
    for char in text or "":
        if char in "。！？!?…\n":
            parts.append("".join(buffer))
            buffer = []
        else:
            buffer.append(char)
    parts.append("".join(buffer))
    return sum(1 for part in parts if part.strip())


def measure(response: Any) -> Dict[str, Any]:
    """一次回复 → 四个观测量（字数 / 句数 / recall / 先说话再调工具）。"""
    content = str(getattr(response, "content", "") or "")
    names: List[str] = []
    for call in getattr(response, "tool_calls", None) or []:
        function = getattr(call, "function", None)
        name = getattr(function, "name", None) if function is not None else None
        if name:
            names.append(str(name))
    return {
        "chars": len(content),
        "sentences": sentence_count(content),
        "tool_calls": names,
        "recall": "recall" in names,
        "spoke_before_tool": bool(content.strip()) and bool(names),
    }


def load_personas(persona_dir: Path) -> List[Dict[str, Any]]:
    """取 personas/ 里的角色卡（最多前 3 张）；读不了的不让脚本崩。"""
    cards: List[Dict[str, Any]] = []
    for path in persona_files(persona_dir)[:MAX_PERSONAS]:
        try:
            card = read_persona(path)
        except Exception as exc:                 # 坏卡跳过，不拖垮整轮对比
            print("跳过读不了的角色卡 %s：%s" % (path.name, exc))
            continue
        card["_id"] = path.stem
        cards.append(card)
    return cards


# ── 请求 ────────────────────────────────────────────────

def _client():
    """按现有设置建大脑；配不齐返回 None（调用方提示后退出）。"""
    from core import llm_runtime

    return llm_runtime.build_default_client()


def _ask(client: Any, prompt: str, message: str, tool_defs: List[dict]) -> Dict[str, Any]:
    """一次非流式请求（与 /api/talk 同样带工具与思考档位）。"""
    from core import llm_runtime

    response = client.chat([{"role": "system", "content": prompt},
                            {"role": "user", "content": message}],
                           tools=tool_defs,
                           reasoning_effort=llm_runtime.thinking_effort())
    return measure(response)


def _aggregate(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(rows)
    if not n:
        return {"n": 0, "avg_chars": 0.0, "avg_sentences": 0.0,
                "recall_calls": 0, "spoke_before_tool": 0, "tool_calls": 0}
    return {
        "n": n,
        "avg_chars": round(sum(row["chars"] for row in rows) / n, 1),
        "avg_sentences": round(sum(row["sentences"] for row in rows) / n, 1),
        "recall_calls": sum(1 for row in rows if row["recall"]),
        "spoke_before_tool": sum(1 for row in rows if row["spoke_before_tool"]),
        "tool_calls": sum(len(row.get("tool_calls") or []) for row in rows),
    }


def run(personas: List[Dict[str, Any]], client: Any,
        inputs: Optional[List[str]] = None) -> Dict[str, Any]:
    """新旧提示词各跑一遍 → 结果字典（Markdown 与 JSON 都由它渲染）。"""
    messages = list(inputs or INPUTS)
    tool_defs = tools.build_tool_definitions(CAPABILITIES)
    result: Dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "model": getattr(client, "model", "") or "",
        "personas": [],
        "inputs": messages,
        "prompt_chars": {"legacy": 0, "current": 0},
        "requests": 0,
    }
    for card in personas:
        legacy_prompt = build_legacy_prompt(card)
        current_prompt = build_system_prompt(card)
        entry: Dict[str, Any] = {
            "npc_id": card.get("_id") or card.get("id") or "",
            "prompt_chars": {"legacy": len(legacy_prompt),
                             "current": len(current_prompt)},
            "prompt_tokens": {"legacy": estimate_tokens(legacy_prompt),
                              "current": estimate_tokens(current_prompt)},
            "rows": [],
        }
        for text in messages:
            row: Dict[str, Any] = {"message": text}
            for name, prompt in (("legacy", legacy_prompt), ("current", current_prompt)):
                row[name] = _ask(client, prompt, text, tool_defs)
                result["requests"] += 1
            entry["rows"].append(row)
        entry["summary"] = {
            "legacy": _aggregate([row["legacy"] for row in entry["rows"]]),
            "current": _aggregate([row["current"] for row in entry["rows"]]),
        }
        result["personas"].append(entry)
    if result["personas"]:
        first = result["personas"][0]
        result["prompt_chars"] = dict(first["prompt_chars"])
        result["prompt_tokens"] = dict(first["prompt_tokens"])
    return result


# ── 输出 ────────────────────────────────────────────────

def _cell(metrics: Dict[str, Any]) -> str:
    recall = "recall 是" if metrics["recall"] else "recall 否"
    lead = "先说话 是" if metrics["spoke_before_tool"] else "先说话 否"
    return "%d 字 / %d 句 / %s / %s" % (metrics["chars"], metrics["sentences"], recall, lead)


def render_markdown(result: Dict[str, Any]) -> str:
    """两栏并排的对比 Markdown（旧在左、现在右）。"""
    chars = result.get("prompt_chars") or {}
    tokens = result.get("prompt_tokens") or {}
    delta_chars = (chars.get("current", 0) - chars.get("legacy", 0))
    delta_tokens = (tokens.get("current", 0) - tokens.get("legacy", 0))
    lines: List[str] = [
        "# 提示词前后对比（旧规矩 vs 现规矩）",
        "",
        "- 生成时间：%s" % result.get("generated_at", ""),
        "- 模型：%s" % (result.get("model") or "（未记录）"),
        "- 角色卡：%s" % ("、".join(entry["npc_id"] for entry in result["personas"]) or "（无）"),
        "- 请求数：%d 次（%d 句输入 × 2 套提示词）"
        % (result.get("requests", 0), len(result.get("inputs") or [])),
        "- 提示词长度：旧 %d 字 / 新 %d 字（差 %+d 字）；约 旧 %d / 新 %d token（差 %+d）"
        % (chars.get("legacy", 0), chars.get("current", 0), delta_chars,
           tokens.get("legacy", 0), tokens.get("current", 0), delta_tokens),
        "",
        "> 动作工具只声明不执行；本脚本不写任何 NPC 的存储数据。",
    ]
    if len(result["personas"]) < MAX_PERSONAS:
        lines.append("> 注意：personas/ 里只有 %d 张可用角色卡（脚本最多取 %d 张）。"
                     % (len(result["personas"]), MAX_PERSONAS))
    for entry in result["personas"]:
        lines.extend([
            "",
            "## %s" % entry["npc_id"],
            "",
            "| # | 玩家说 | 旧规矩 | 现规矩 |",
            "|---|--------|--------|--------|",
        ])
        for index, row in enumerate(entry["rows"], 1):
            text = row["message"].replace("|", "\\|")
            lines.append("| %d | %s | %s | %s |"
                         % (index, text, _cell(row["legacy"]), _cell(row["current"])))
        summary = entry["summary"]
        old, new = summary["legacy"], summary["current"]
        lines.extend([
            "",
            "**汇总（%d 句）**" % old["n"],
            "",
            "| 指标 | 旧规矩 | 现规矩 |",
            "|------|--------|--------|",
            "| 平均字数 | %s | %s |" % (old["avg_chars"], new["avg_chars"]),
            "| 平均句数 | %s | %s |" % (old["avg_sentences"], new["avg_sentences"]),
            "| 调了 recall 的次数 | %d | %d |" % (old["recall_calls"], new["recall_calls"]),
            "| 先说话再调工具 | %d | %d |" % (old["spoke_before_tool"], new["spoke_before_tool"]),
            "| 工具调用总数 | %d | %d |" % (old["tool_calls"], new["tool_calls"]),
        ])
    return "\n".join(lines) + "\n"


def _print_dry(personas: List[Dict[str, Any]]) -> int:
    """--dry：只算请求数与提示词长度差，不发请求、不需要 key。"""
    if not personas:
        print("personas/ 里没有可用角色卡，无法对比。")
        return 2
    total_chars = {"legacy": 0, "current": 0}
    total_tokens = {"legacy": 0, "current": 0}
    print("将发出 %d 次请求（%d 张角色卡 × %d 句 × 2 套提示词）"
          % (len(personas) * len(INPUTS) * 2, len(personas), len(INPUTS)))
    if len(personas) < MAX_PERSONAS:
        print("注意：personas/ 里只有 %d 张可用角色卡（脚本最多取 %d 张）"
              % (len(personas), MAX_PERSONAS))
    for card in personas:
        legacy = build_legacy_prompt(card)
        current = build_system_prompt(card)
        old_chars, new_chars = len(legacy), len(current)
        old_tokens, new_tokens = estimate_tokens(legacy), estimate_tokens(current)
        total_chars["legacy"] += old_chars
        total_chars["current"] += new_chars
        total_tokens["legacy"] += old_tokens
        total_tokens["current"] += new_tokens
        print("  %-10s 旧 %4d 字 / 新 %4d 字（差 %+d）；约 旧 %4d / 新 %4d token（差 %+d）"
              % (card.get("_id") or card.get("id") or "?",
                 old_chars, new_chars, new_chars - old_chars,
                 old_tokens, new_tokens, new_tokens - old_tokens))
    print("合计：旧 %d 字 / 新 %d 字（差 %+d）；约 旧 %d / 新 %d token（差 %+d）"
          % (total_chars["legacy"], total_chars["current"],
             total_chars["current"] - total_chars["legacy"],
             total_tokens["legacy"], total_tokens["current"],
             total_tokens["current"] - total_tokens["legacy"]))
    print("（--dry 只算长度，不发请求；去掉 --dry 才会真跑）")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="提示词前后对比（旧规矩 vs 现规矩）")
    parser.add_argument("--dry", action="store_true",
                        help="只打印将发出的请求数与提示词长度差（不发请求）")
    parser.add_argument("--out", default="",
                        help="输出目录（默认 out/prompt_compare/<时间戳>）")
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except Exception:
                pass

    personas = load_personas(ROOT / "personas")
    if args.dry:
        return _print_dry(personas)

    if not personas:
        print("personas/ 里没有可用角色卡，无法对比。")
        return 2
    if not env_api_key():
        print("未检测到真实 key：请配置 NPC_API_KEY（或 api_key.txt / 控制台「模型」页），再跑对比。")
        print("只想看提示词长度差可以加 --dry（不发请求）。")
        return 2
    client = _client()
    if client is None:
        print("LLM 三件套未配齐（模型名 / key / 端点），对比需要真实模型。")
        return 2

    result = run(personas, client)
    out_dir = Path(args.out) if args.out else ROOT / "out" / "prompt_compare" / time.strftime("%Y%m%d_%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "compare.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown = render_markdown(result)
    (out_dir / "compare.md").write_text(markdown, encoding="utf-8")
    print(markdown)
    print("输出：%s（compare.md / compare.json）" % out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
