"""
NPCSidekick — 子代理运行器（§17, 2026-08-24）: B2 编译 / A 审查的 LLM 化外壳。

参考（GitHub 实读 2026-08-24）:
  - DeepSeek Harness subagent seam（spawn 新鲜眼 / 结构化结果四件套 /
    非 completed 一律按失败处理 / 响亮失败不静默降级）
  - OpenAI Codex custom agents（窄而专的 developer_instructions /
    max_depth 防递归 / worker 必须恰好报告一次）

映射到本项目三角色架构:
  B1 对话  — 主代理（嘴），已有（npc.talk）
  B2 编译  — 子代理: 把对话里的承诺编译成任务单（产出物过审后经唯一落账口 book()）
  A  审查  — 子代理: 语义层审查（人设/编造/空口承诺），叠加在规则层之上

四条纪律（与 harness/codex 对齐）:
  1. spawn 新鲜眼     — 每次调用独立简报上下文，绝不继承 B1 的完整对话
                        （审查只看产出、不看生成方推理 — 本项目铁律的同款表述）
  2. 结构化契约       — 输出必须是 JSON 对象；解析失败/为空 → schema_invalid/refusal，
                        绝不把半截文本当成功
  3. 失败语义分角色   — B2 失败 → 不落账（承诺成立=已落账，铁律天然兜底）;
                        A 失败 → 放行（审查失败/超时不卡对话 — 项目既有铁律）
  4. 窄而专·零递归    — 每个子代理一份窄 system 提示；纯函数无工具环，
                        depth=0 由构造保证

开关（代码默认关，bat/环境变量逐个打开 — 保住存量行为零回归）:
  NPC_SUBAGENT=0        总闸（杀全部子代理）
  NPC_SUBAGENT_B2=1     打开 B2 编译子代理
  NPC_SUBAGENT_A=1      【已退役·§22】读到仅告警忽略; A 机封存于 _a_semantic_block
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from npc.scheduler import P_COMPILE, P_REVIEW, SCHED

# ── stop_reason 词表（对齐 DeepSeek Harness SubagentStopReason 思路）──
STOP_COMPLETED = "completed"        # 正常完成且 JSON 合法
STOP_SCHEMA_INVALID = "schema_invalid"  # 有输出但不是合法 JSON 对象
STOP_REFUSAL = "refusal"            # 空内容
STOP_ERROR = "error"                # 调用异常（网络/限流等）
STOP_NO_LLM = "no_llm"              # 该角色没有可用模型通道（未尝试）
STOP_DISABLED = "disabled"          # 被开关关掉（未尝试）


@dataclass
class SubagentSpec:
    """一个子代理的身份卡（Codex custom agent 的 TOML 三件套同款思路）。"""
    name: str                     # 观测名: "b2_compiler" / "a_reviewer"
    tag: str                      # 日志标签: "B2" / "A"
    system: str                   # 窄人设（只管一件事）
    role: str = "review"          # 走哪条模型通道（_get_llm 的 role）
    reasoning_effort: Optional[str] = "low"


@dataclass
class SubagentResult:
    """终态结果四件套（对齐 SubagentResult: structured/raw/diagnostic/stopReason）。"""
    ok: bool
    stop_reason: str
    data: Optional[Dict] = None   # completed 时才有: 解析出的 JSON 对象
    raw: str = ""                 # 原始输出截断（诊断用，不进提示词）
    error: str = ""               # 异常摘要
    latency_ms: int = 0


# ── 观察者钩子: server 注册进来记 /api/stats（子代理模块不反向依赖 server）──
Observer = Callable[[str, SubagentResult, str], None]   # (name, result, npc_id)
_observer: Optional[Observer] = None


def set_observer(fn: Optional[Observer]) -> None:
    global _observer
    _observer = fn


def subagent_enabled(tag: str) -> bool:
    """开关读取（每次调用现读环境变量 — 测试/运行时可热切）。代码默认关。"""
    import os
    if os.environ.get("NPC_SUBAGENT", "") == "0":
        return False
    return os.environ.get(f"NPC_SUBAGENT_{tag}", "") == "1"


def _parse_json_object(text: str) -> Optional[Dict]:
    """宽容解析: 剥 ```json 围栏 → 取首尾大括号 → json.loads。不是对象 → None。"""
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t).strip()
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        obj = json.loads(t[i:j + 1])
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _emit(npc, tag: str, text: str) -> None:
    """生命周期事件写世界日志 → server 解析成 SSE/polling 事件（调试台可见）。"""
    try:
        npc.world.setdefault("log", []).append(f"[{tag}] {npc.actor_id} {text}")
    except Exception:
        pass   # 日志是观测通道，绝不能反噬主流程


def run_subagent(npc, spec: SubagentSpec, brief: str) -> SubagentResult:
    """单次委派: 独立简报 → 角色通道 LLM → JSON 契约校验 → 四件套结果。

    disabled/no_llm 直接返回（不计观测 — 没发生真实调用就没有统计噪声）。
    """
    if not subagent_enabled(spec.tag):
        return SubagentResult(False, STOP_DISABLED)
    llm = npc._get_llm(role=spec.role)
    if llm is None:
        return SubagentResult(False, STOP_NO_LLM)

    _emit(npc, spec.tag, f"{spec.name} 开始")
    t0 = time.time()
    messages: List[Dict[str, str]] = [
        {"role": "system", "content": spec.system},
        {"role": "user", "content": brief},
    ]
    try:
        prio = P_COMPILE if spec.tag == "B2" else P_REVIEW
        resp = SCHED.invoke(prio, llm.chat, messages,
                            reasoning_effort=spec.reasoning_effort, timeout=60.0)
        text = (getattr(resp, "content", "") or "").strip()
        latency = int((time.time() - t0) * 1000)
        if not text:
            res = SubagentResult(False, STOP_REFUSAL, latency_ms=latency)
        else:
            data = _parse_json_object(text)
            if data is None:
                res = SubagentResult(False, STOP_SCHEMA_INVALID,
                                     raw=text[:400], latency_ms=latency)
            else:
                res = SubagentResult(True, STOP_COMPLETED, data=data,
                                     raw=text[:400], latency_ms=latency)
    except Exception as exc:   # 网络/限流/任何故障 — 记录后按各自角色的失败语义处理
        res = SubagentResult(False, STOP_ERROR, error=str(exc)[:200],
                             latency_ms=int((time.time() - t0) * 1000))

    _emit(npc, spec.tag, f"{spec.name} {'✓ ' + str(len(res.data or {})) + ' 字段' if res.ok else '✗ ' + res.stop_reason}")
    if _observer is not None:
        try:
            _observer(spec.name, res, getattr(npc, "actor_id", "?"))
        except Exception:
            pass
    return res


# ── B2 编译子代理 ─────────────────────────────────────────────
B2_SYSTEM = (
    "你是游戏NPC的任务编译器。你只做一件事: 判断这段对话里 NPC 是否答应了一件"
    "清单内的可执行任务，并把它编译成 JSON 任务单。不聊天、不解释、不发挥。"
    "只输出一个 JSON 对象，格式如 {\"action\":\"gather\",\"resource\":\"<资源名>\",\"count\":2}；"
    "识别不到清单内任务就输出 {\"action\":null}。"
    "禁止发明清单外的动作，禁止任何文件/路径/系统类操作。"
)


def b2_brief(player_input: str, reply: str, manifest: Dict, lexicon: List[str]) -> str:
    """独立简报: 动作清单 + 资源词典 + 玩家输入 + NPC 回复（不给完整对话史）。"""
    lines = [f"玩家说：{player_input}", f"NPC回复：{reply}", "可用动作清单："]
    for action, m in (manifest or {}).items():
        lines.append(f"- {action}: 参数{m.get('params', [])}；{m.get('desc', '')}")
    if lexicon:
        lines.append("已知资源名：" + "、".join(lexicon))
    lines.append('只输出 JSON 任务单；清单里没有能对应的事 → {"action":null}。')
    return "\n".join(lines)


# ── A 语义审查子代理 ──────────────────────────────────────────
A_SYSTEM = (
    "你是NPC对话审查员。判断这条NPC回复是否合格: 是否违背人设禁忌、编造没发生的"
    "事、许下兑不了的承诺。只输出一个 JSON 对象:"
    " {\"block\": true/false, \"reason\":\"一句话原因\"}。"
    "拿不准一律 block=false（硬约束由规则层兜底，你只拦语义层的病）。"
)


def a_brief(identity: str, taboos: List[str], player_input: str,
            reply: str, booked_desc: str) -> str:
    """独立简报: 人设要点 + 输入 + 回复 + 落账状态（新鲜眼，不看 B1 的推理）。"""
    lines = [f"NPC人设：{identity}",
             f"禁忌：{'、'.join(taboos) if taboos else '无'}",
             f"已落账的任务：{booked_desc or '无'}",
             f"玩家说：{player_input}",
             f"NPC回复：{reply}",
             '只输出 {"block": ...} JSON。']
    return "\n".join(lines)
