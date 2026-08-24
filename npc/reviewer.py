"""
NPCSidekick — 三角色: Agent A 审查管线 + Agent B 任务编译。

三角色（模块化双角色，2026-08-21 架构决策）:
  B1 对话  — LLM 生成回复文本（表象）
  B2 编译  — 把玩家诉求编译成任务单（唯一落账口）
  A  审查  — 合规 + 可行性拦截（按场景触发）

铁律（论文《The LLM Proposes, the Executive Disposes》+ 项目实测）:
  - 承诺成立 = 指令通过 A 审查并被 B2 推进活动队列，不是嘴上那番话
  - 审查只看产出、不看生成方推理（独立上下文，防确认偏差）
  - 审查失败/超时 → 放行，绝不卡住对话（规则保底）
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

# ── B2 编译: 玩家意图词表 ─────────────────────────────
_INTENT_WORDS = ("给我", "给", "要", "需要", "帮我", "弄点", "去砍", "去采")

# 内置资源词典（默认兜底 = 本游戏动作; 有世界/清单声明时被动态词典替换）
DEFAULT_RESOURCE_ALIASES: Dict[str, tuple] = {
    "木材": ("木材", "木头", "柴", "木", "树"),
    "浆果": ("浆果", "果"),
    "石头": ("石头", "石"),
}

# v2026-08-23 动态资源词典:
#   None = 未加载 → 用内置默认表（行为与旧版完全一致, 现有测试零改动）;
#   非 None = 从 world["locations"] 动态收集(load_resource_lexicon_from_world) —
#   游戏加新资源只改世界 JSON, 对话接单立刻认识, 一劳永逸。
_ACTIVE_RESOURCE_ALIASES: Optional[Dict[str, tuple]] = None

_CN_NUM = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def load_resource_lexicon_from_world(world: Optional[Dict],
                                     extra: Optional[Dict[str, list]] = None) -> Dict[str, tuple]:
    """从世界状态动态收集资源词典（规范名 → 同义词族），并设为当前生效。

    收集规则:
      - 遍历 world["locations"][*]["resources"] 的资源名（含余量 0 — 采空了也
        还能接单聊, 真采不到由 A 审查可行性诚实拒绝, 不归词典管）;
      - 别名来源: 世界扩展键 world["_resource_aliases"] = {规范名: [别名...]}
        + 动作清单(manifest)里的 resources 段(经 extra 合并);
    语义:
      - 收集到 ≥1 个资源 → 整表替换默认（游戏声明自己的真相, 不让别游戏的
        残留词漏进来）; 纯对话世界(GTA 一个资源都没有)→ 回退默认表;
      - world=None 且 extra 空 → 恢复默认（测试复位用）。
    返回生效词典。
    """
    global _ACTIVE_RESOURCE_ALIASES
    if world is None and not extra:
        _ACTIVE_RESOURCE_ALIASES = None
        return dict(DEFAULT_RESOURCE_ALIASES)
    lex: Dict[str, List[str]] = {}
    for loc in ((world or {}).get("locations") or {}).values():
        if not isinstance(loc, dict):
            continue
        for name in (loc.get("resources") or {}):
            if isinstance(name, str) and name:
                lex.setdefault(name, [name])
    # manifest resources 段: {规范名: [别名...]} — 规范名不在世界里也收（清单即真相）
    for canon, aliases in (extra or {}).items():
        canon = str(canon)
        bucket = lex.setdefault(canon, [canon])
        for a in (aliases or []):
            a = str(a)
            if a and a not in bucket:
                bucket.append(a)
    # 世界显式别名: 只给已收集到的规范名补别名, 不凭空造资源
    for canon, aliases in (((world or {}).get("_resource_aliases")) or {}).items():
        canon = str(canon)
        if canon in lex:
            for a in (aliases or []):
                a = str(a)
                if a and a not in lex[canon]:
                    lex[canon].append(a)
    active = {k: tuple(v) for k, v in lex.items()}
    _ACTIVE_RESOURCE_ALIASES = active if active else None
    return dict(_ACTIVE_RESOURCE_ALIASES or DEFAULT_RESOURCE_ALIASES)


def get_resource_aliases() -> Dict[str, tuple]:
    """当前生效的资源词典（动态优先, 未加载用默认）。"""
    return _ACTIVE_RESOURCE_ALIASES if _ACTIVE_RESOURCE_ALIASES else dict(DEFAULT_RESOURCE_ALIASES)


# ── A 审查: 对话承诺词 / 高风险话题 ─────────────────────
# "我去/我这就…" = 答应去做某事 → 必须已落账（否则 = 口是心非）
_PROMISE_WORDS = ("我去", "我这就", "这就去", "马上", "就给你", "给你弄",
                  "给你采", "给你拿", "帮你办", "办妥", "等着")
# 交付承诺词 (L3 高影响): 回复承诺"给/送到"玩家手上 → 必须已有 deliver 落账
_DELIVER_PROMISE_WORDS = ("给你", "送到", "交到你", "送到你", "给你送", "给你带",
                          "弄给你", "拿来给你", "去给你")
_RISKY_TOPICS = ("杀", "打", "偷", "骗", "伤", "毒", "火", "淹", "摔", "抢")

# A 审查拦截后喂回给 B1 的重生成提示
REVIEW_RETRY_HINT = (
    "（审查拦截：你刚才的话答应了去做一件事，但这件事没有登记进执行队列。"
    "不要承诺任何没登记的事；要么只闲聊，要么让玩家明确说'给我/帮我 + 数量 + 东西'。"
    "重新回答，不要再说要去做某件事。）"
)

# 审查场景: "command"=接单（必审）; "dialogue"=对话（按场景触发）
REVIEW_SCENES = ("command", "dialogue")

# 防篡改: B2 允许编译的任务动作白名单（游戏内动作，绝无文件/路径/执行类）。
# 2026-08-21 清单化重构: 动作集不再写死, 由「动作清单 manifest」驱动。
#   · 默认清单 = 本游戏动作（gather/craft/deliver/move），行为与原版完全一致;
#   · 其他游戏可 load_manifest(自己清单) 换成自己的动作 →
#     白名单 / 审批表 / 影响等级 全部随之变化（A 审查只认清单里的动作）;
#   · load_manifest(None) 恢复默认, 保证向后兼容。
# 清单字段: tier(1低/2中/3高影响 ~ Codex read-only/workspace-write/danger-full-access)
#           approval(allow/ask/deny) + params(参数名, 描述用)
# 审批全局档位 (Codex approval policy 对照):
#   "auto"       = 按动作等级自动审 (L3 必审 + 承诺必审)
#   "on-failure" = 宽松: 只靠执行失败兜底（演示用）
#   "never"      = 跳过审查直接落账（仅测试/无 LLM 时）
APPROVAL_POLICY_VALUES = ("auto", "on-failure", "never")

# Approval 策略三态（Codex /approvals 对照, 2026-08-21）:
#   allow = 自动放行（白名单/可行性底线仍生效）
#   ask   = 需要审查（A 审查 + 承诺落账深审）
#   deny  = 直接拒绝（连审查都不走, 不落账）
APPROVE_ALLOW = "allow"
APPROVE_ASK = "ask"
APPROVE_DENY = "deny"
APPROVE_DECISIONS = (APPROVE_ALLOW, APPROVE_ASK, APPROVE_DENY)

# 默认动作清单（= 本游戏动作; 其他游戏替换成自己的）
DEFAULT_ACTION_MANIFEST: Dict[str, Dict] = {
    "gather":  {"tier": 1, "approval": APPROVE_ALLOW, "params": ["resource", "count"], "desc": "采集资源(只影响自己背包)"},
    "craft":   {"tier": 2, "approval": APPROVE_ASK,   "params": ["recipe", "count"],   "desc": "制作(改变自身持有)"},
    "deliver": {"tier": 3, "approval": APPROVE_ASK,   "params": ["resource", "count"], "desc": "交付(改变玩家背包,最高影响)"},
    "move":    {"tier": 2, "approval": APPROVE_ASK,   "params": ["x", "z"],            "desc": "移动(改变自身位置)"},
}

# —— 由默认清单派生的常量（名字/值与原版一致 → 现有调用方与测试零改动）——
ALLOWED_TASK_ACTIONS: tuple = tuple(DEFAULT_ACTION_MANIFEST.keys())
ACTION_LEVELS: Dict[str, int] = {a: int(m["tier"]) for a, m in DEFAULT_ACTION_MANIFEST.items()}
ACTION_APPROVAL: Dict[str, str] = {a: m["approval"] for a, m in DEFAULT_ACTION_MANIFEST.items()}

# 当前生效清单（None = 默认）; 会话内动态覆盖表保留原逻辑
_ACTIVE_MANIFEST: Optional[Dict] = None
_ACTION_APPROVAL_OVERRIDE: Dict[str, str] = {}

# 动作清单文件里的 resources 段（manifest 加载时暂存, 世界就绪后并入资源词典）
_MANIFEST_RESOURCES: Optional[Dict[str, list]] = None


def set_manifest_resources(resources: Optional[Dict[str, list]]) -> None:
    """暂存动作清单文件的 resources 段（{规范名: [别名...]}），None 清空。"""
    global _MANIFEST_RESOURCES
    _MANIFEST_RESOURCES = dict(resources) if resources else None


def get_manifest_resources() -> Optional[Dict[str, list]]:
    """动作清单声明的资源别名段（server 在世界就绪后传给 load_resource_lexicon_from_world）。"""
    return _MANIFEST_RESOURCES


def load_manifest(manifest: Optional[Dict]) -> None:
    """设置当前生效的动作清单（每游戏一份）。None → 恢复默认(内置清单)。

    校验: 每个动作必须是 dict, 需含合法 tier(1..3) 与 approval(allow/ask/deny)。
    非法清单抛 ValueError, 原清单保持不变（不会半套生效）。
    """
    global _ACTIVE_MANIFEST
    if manifest is None:
        _ACTIVE_MANIFEST = None
        return
    if not isinstance(manifest, dict) or not manifest:
        raise ValueError("manifest 必须是含至少一个动作的 dict")
    cleaned: Dict[str, Dict] = {}
    for action, spec in manifest.items():
        if not isinstance(spec, dict):
            raise ValueError(f"manifest 动作 {action} 必须是对象 {{tier, approval}}")
        tier = int(spec.get("tier", 2))
        if tier not in (1, 2, 3):
            raise ValueError(f"manifest 动作 {action}.tier 必须是 1/2/3，实际 {tier}")
        if spec.get("approval") not in APPROVE_DECISIONS:
            raise ValueError(f"manifest 动作 {action}.approval 必须是 allow/ask/deny")
        cleaned[action] = {
            "tier": tier,
            "approval": spec["approval"],
            "params": list(spec.get("params", [])),
            "desc": str(spec.get("desc", "")),
        }
    _ACTIVE_MANIFEST = cleaned


def parse_manifest_doc(doc: Dict) -> Dict:
    """解析动作清单文档 → {"actions": {...}, "resources": {...}}（正式 Schema v2026-08-23）。

    兼容两种形态:
      - 新版完整模板: {"actions": {...}, "resources": {规范名: [别名...]}}（examples/actions.json）
      - 旧版裸动作表: {"gather": {...}, ...}（直接就是动作 dict）
    返回统一结构; actions 缺失/非法抛 ValueError（调用方决定回退默认）。
    """
    if not isinstance(doc, dict):
        raise ValueError("动作清单必须是 JSON 对象")
    if "actions" in doc:
        actions = doc.get("actions")
        if not isinstance(actions, dict) or not actions:
            raise ValueError("actions 必须是非空对象")
        resources = doc.get("resources")
        if resources is not None and not isinstance(resources, dict):
            raise ValueError("resources 必须是对象 {规范名: [别名...]}")
        return {"actions": actions, "resources": resources}
    # 旧版裸动作表（启发式: 含 tier/approval 键的才像动作 spec）
    if all(isinstance(v, dict) and ("tier" in v or "approval" in v) for v in doc.values()):
        return {"actions": doc, "resources": None}
    raise ValueError("无法识别的清单格式: 需要 {'actions': {...}} 或裸动作表")


def get_manifest() -> Dict:
    """当前生效清单（/api/manifest GET 用）: {action: {tier, approval, params, desc}}。"""
    src = _ACTIVE_MANIFEST if _ACTIVE_MANIFEST is not None else DEFAULT_ACTION_MANIFEST
    return {a: dict(m) for a, m in src.items()}


def manifest_is_default() -> bool:
    return _ACTIVE_MANIFEST is None


def _current_actions() -> tuple:
    return tuple(_ACTIVE_MANIFEST.keys()) if _ACTIVE_MANIFEST else ALLOWED_TASK_ACTIONS


def _current_approval() -> Dict:
    if _ACTIVE_MANIFEST:
        return {a: m["approval"] for a, m in _ACTIVE_MANIFEST.items()}
    return ACTION_APPROVAL


def _current_levels() -> Dict:
    if _ACTIVE_MANIFEST:
        return {a: int(m["tier"]) for a, m in _ACTIVE_MANIFEST.items()}
    return ACTION_LEVELS


def set_approval(action: str, decision: str,
                 overrides: Optional[Dict[str, str]] = None) -> bool:
    """运行时调整单动作审批策略（Codex /approvals 对照）。不合法 → False。

    overrides 缺省 = 全局会话覆盖表；传 NPC 实例表 = 按 NPC 粒度配置。
    设为该动作的默认策略 = 恢复默认（清除覆盖），避免残留污染后序判定。
    """
    _appr = _current_approval()
    if action not in _appr or decision not in APPROVE_DECISIONS:
        return False
    target = overrides if overrides is not None else _ACTION_APPROVAL_OVERRIDE
    if decision == _appr[action]:
        target.pop(action, None)   # 还原默认 → 清除覆盖
    else:
        target[action] = decision
    return True


def approve_action(action: str, approval_policy: str = "auto",
                   overrides: Optional[Dict[str, str]] = None) -> str:
    """按动作返回审批决定: allow / ask / deny。

    优先级: 实例覆盖(NPC) > 会话内动态覆盖 > 环境变量 NPC_APPROVAL_<ACTION> > 默认表。
    全局档位: never/on-failure → 全 allow（仅测试/演示/宽松模式）。
    Codex 对照: deny 直接拒绝（连审查都不走）, ask 走 A 审查, allow 放行。
    """
    if approval_policy in ("never", "on-failure"):
        return APPROVE_ALLOW
    if overrides and action in overrides:
        return overrides[action]
    if action in _ACTION_APPROVAL_OVERRIDE:
        return _ACTION_APPROVAL_OVERRIDE[action]
    import os
    override = os.environ.get(f"NPC_APPROVAL_{action.upper()}")
    if override in APPROVE_DECISIONS:
        return override
    return _current_approval().get(action, APPROVE_ASK)


def approval_table(approval_policy: str = "auto",
                   overrides: Optional[Dict[str, str]] = None) -> Dict:
    """当前生效策略视图（/api/approval GET 用）: {action: decision}。"""
    return {a: approve_action(a, approval_policy, overrides) for a in _current_approval()}


# 防篡改: 任务里出现这些字段名/值的字段 → 审查直接拒绝（纵深防御）
_FORBIDDEN_TASK_TOKENS = frozenset({"file", "path", "write", "read", "exec",
                                    "shell", "delete", "open"})


def should_review(scene: str, reply: str = "", player_input: str = "",
                  approval_policy: str = "auto") -> bool:
    """按场景触发审查（成本闸门）: 接单按策略; 对话只在承诺/高风险时深审。

    approval_policy (Codex approval policy 对照):
      "auto"       = 接单必审（默认，安全）
      "on-failure" = 接单跳过预审，靠执行失败兜底
      "never"      = 接单不审（仅测试/演示，不安全）
    """
    if scene == "command":
        return approval_policy == "auto"
    if scene == "dialogue":
        return _promise_like(reply) or _risky_topic(player_input)
    return False


def looks_like_intent(text: str) -> bool:
    """宽触发（§17 B2 子代理用）: 输入含意图词即算"像派活"。

    规则快路径仍走 compile_task 的"意图词+资源词"双命中防误接；
    这个宽版只决定要不要叫 B2 子代理来看一眼（多一次调用，不改变落账门槛）。
    """
    return any(w in text for w in _INTENT_WORDS)


def _promise_like(text: str) -> bool:
    return any(w in text for w in _PROMISE_WORDS)


def _risky_topic(text: str) -> bool:
    return any(w in text for w in _RISKY_TOPICS)


def action_level(action: str) -> int:
    """动作影响等级 (1 低 / 2 中 / 3 高)。未知动作 = 最高级(拒绝)。"""
    return _current_levels().get(action, 3)


def needs_deep_review(reply: str, approval_policy: str = "auto") -> bool:
    """回复承诺了交付类行为 (L3 高影响) 且在 auto 策略下 → 需 A 深度审查。

    Codex danger-full-access 对照: 最高权限动作不自动放行。
    深度审查 = 承诺交付必须已落账 deliver 任务 (review_dialogue 保证)。
    """
    if approval_policy != "auto":
        return False
    return any(w in reply for w in _DELIVER_PROMISE_WORDS)


def _hit_taboo(persona: Dict, text: str) -> Optional[str]:
    for taboo in persona.get("taboos", []):
        if taboo in text:
            return taboo
    return None


# ── B2: 编译任务单（玩家诉求 → 结构化任务）──────────────
def compile_task(player_input: str) -> Optional[Dict]:
    """B2 编译: 玩家输入 → 任务单 {action, resource, count}。识别不到 → None。

    只识别"意图词 + 资源词"双命中，避免误接（"我要去散步"不触发）。
    数量: "两根"/"2个" → 数字；默认 1。
    资源词典 = 动态优先（load_resource_lexicon_from_world 加载过的世界真相），
    未加载时退回内置默认表 — 游戏加新资源改世界 JSON 即生效,零代码。
    """
    if not any(w in player_input for w in _INTENT_WORDS):
        return None
    for resource, aliases in get_resource_aliases().items():
        if not any(a in player_input for a in aliases):
            continue
        count = 1
        m = re.search(r"([0-9]+|[一二两三四五六七八九十]+)\s*个?", player_input)
        if m:
            raw = m.group(1)
            count = _CN_NUM.get(raw, int(raw) if raw.isdigit() else 1)
        return {"action": "gather", "resource": resource, "count": max(1, count)}
    return None


# ── A: 审查任务（安全 + 可行性，规则）────────────────────
def review_task(npc, task: Dict) -> tuple[bool, str]:
    """A 审查: 任务单（规则）— 安全白名单 + 可行性。不过 → (False, 拒绝语)。

    防篡改铁律: LLM 只提议、代码决定执行 — 审查只放行白名单游戏动作，
    含路径/文件/读写/执行类字段的任务一律拒绝（LLM 永远碰不到文件）。
    可行性: 资源枯竭 → 诚实拒绝，不空口答应。
    """
    action = task.get("action", "")
    if action not in _current_actions():
        return False, f"……这个我不会做（不允许的动作：{action}）。"
    if _FORBIDDEN_TASK_TOKENS & {str(f).lower() for f in task}:
        return False, "……这个涉及我不该碰的东西。"
    for v in task.values():
        if isinstance(v, str) and any(t in v.lower() for t in _FORBIDDEN_TASK_TOKENS):
            return False, "……这个涉及我不该碰的东西。"
    resource = task.get("resource", "")
    from npc.scheduler import resource_site

    if resource_site(npc.world, npc.actor_pos, resource) is None:
        return False, f"……{resource}现在弄不到了，采空了，等它长回来吧。"
    return True, ""


# ── A: 审查对话（合规，规则层）───────────────────────────
def review_dialogue(npc, reply: str, player_input: str) -> tuple[bool, str]:
    """A 审查: LLM 回复（规则层）— 禁忌词 + 承诺必须已落账 + L3 高影响动作深审。

    不过 → (False, 原因); 过 → (True, "")。
    L3 高影响动作(deliver): 承诺交付必须已有对应的 deliver 落账任务 —
    否则即使 pending_task 存在(可能是去采集), 交付承诺也算"口是心非"。
    LLM 深度审查（捏造/人设）是扩展点: 按 should_review("dialogue",...) 触发，
    目前保持规则层（零成本），语义审查留待接模型后启用。
    """
    taboo = _hit_taboo(npc.persona, reply)
    if taboo is not None:
        return False, f"回复触犯禁忌: {taboo}"
    if _promise_like(reply) and npc.pending_task is None:
        return False, "承诺了去办事但任务未落账"
    # L3 高影响动作深审 (Codex danger-full-access 对照: 最高权限不自动放行)
    if needs_deep_review(reply, npc.approval_policy):
        task = npc.pending_task
        # 承诺了交付, 但落账任务不是 deliver → 拦截（可能是去采集还没采到）
        if task is None or task.get("action") != "deliver":
            return False, "承诺交付但落账任务不是交付动作"
    return True, ""
