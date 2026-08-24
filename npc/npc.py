"""
NPCSidekick — NPC 运行时（轻路径）。

设计点 B（复杂度分级）: 普通 NPC 走轻路径 = 规则快路径 + 小模型对话，
不启动完整 Plan→Execute→Reflect（那留给 v4 帮手 NPC）。

设计点 #4（记忆=可编辑文档）: 记忆卡是 JSON 文件 — 用户/玩家可直接打开修改。
重启后加载记忆卡即恢复（验收点: 重启后记得进度）。
"""
from __future__ import annotations

import json
import os
import time
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from agent.logging_config import log
from agent.llm.client import LLMClient
from agent.providers.factory import create_provider
from npc import safety as _safety
from npc import subagent as _sub
from npc.scheduler import P_REFLECT, SCHED
from npc.memory import NPCMemory
from npc.persona import SAMPLE_NPC, build_system_prompt
from npc.reviewer import (REVIEW_RETRY_HINT, APPROVAL_POLICY_VALUES, APPROVE_DENY, compile_task,
                          approve_action, get_manifest, get_resource_aliases, needs_deep_review,
                          looks_like_intent, review_dialogue, review_task,
                          set_approval as reviewer_set_approval, should_review)
from npc.world import apply_action, default_world, find_path, observe

# §22(2026-08-25): A 审查退役 —— 旧开关读到即警告忽略（机器按"乙案"封存于 subagent.py / _a_semantic_block）
import os as _env_probe
if _env_probe.environ.get("NPC_SUBAGENT_A"):
    log.warning("npc_subagent_a_deprecated",
                hint="A 已退役(§22)，NPC_SUBAGENT_A 被忽略；见 docs/NPC大脑架构.md §22")

# 短期对话历史轮数（只记最近 N 轮 — 上下文定期重置；对话不进长期记忆卡防污染，Day 2 铁律）
DIALOGUE_HISTORY_TURNS = 5

# 反思归纳（阶段① 海马体升级）: 未反思记忆重要性之和达阈值 → 触发反思（Generative Agents 同款语义）
REFLECT_IMPORTANCE_THRESHOLD = 12
# 一次反思最多纳入的条目数（防上下文过长）
REFLECT_MAX_ENTRIES = 8


def _dedup_enabled() -> bool:
    """记忆卡治理·总开关 NPC_MEMORY_DEDUP（方案稿获批 2026-08-25，默认关：不设/0 全链路与旧版一致）。

    开启后生效两道闸：
    闸1 写入去重聚合 —— remember() 里同文日常条目就地计数，不再新增重复行；
    闸2 反思卫生 —— 噪音批不产反思、重复反思不重写（防"总结垃圾产生垃圾"）。
    """
    return os.environ.get("NPC_MEMORY_DEDUP", "") not in ("", "0")


def _reflect_rules(entries: List[Dict]) -> str:
    """规则兜底反思: 只做事实摘要，不发明新事实（防 confabulation）。"""
    tops = sorted(entries, key=lambda e: e.get("importance", 0), reverse=True)[:2]
    return "我最近做了这些事：" + "；".join(t["content"] for t in tops)


class NPC:
    """一个普通 NPC。每个 NPC 持有独立的: 人格 + 世界状态 + 记忆卡。"""

    def __init__(
        self,
        persona: Optional[Dict] = None,
        world: Optional[Dict] = None,
        store_dir: str = "npc/store",
        use_llm: bool = True,
        ephemeral: bool = False,
    ):
        self.persona = dict(SAMPLE_NPC if persona is None else persona)
        self.actor_id = self.persona["id"]
        # 流民层(2026-08-22): RAM-only,save() 跳过 — despawn 即忘(陌生人聊一次就忘)。
        # 常驻层(cast 静态角色/GTA locals)默认 False,记忆落盘可续前缘。
        self.ephemeral = ephemeral
        # 共享世界: 传入世界 = 引用（多 NPC 共用一个世界 — AI Town 模式）；
        # 未传入 = 新建独立世界。
        self.world = deepcopy(default_world()) if world is None else world
        from npc.world import actor_of  # 注册角色槽（共享世界时此 NPC 的槽位）

        actor_of(self.world, self.actor_id)
        self.store_path = Path(store_dir) / f"{self.actor_id}_memory.json"
        self.task_log: List[Dict] = []
        self.memory = NPCMemory()          # 加权记忆（AI Town 公式，MIT）
        # 反思进度: 已归纳到第几个记忆条目（不重复反思；随记忆卡落盘）
        self._reflected_upto = 0
        # 自主循环运行时状态（不落盘 — 重启 = 活动清零回 idle 重新规划，AI Town 同款语义）
        self.state = "idle"                # "idle" | "walking" | "working" | "resting"
        self.activity: Optional[Dict] = None   # {"item", "steps", "desc"} — 进行中的日常
        self._blocked: Dict = {}           # {(action, resource): 冷却到第几个 tick}
        # 对话下的指令（"给我两根木材" → 村民真去干，tick 循环一步步执行）
        self.pending_task: Optional[Dict] = None   # {"action","resource","count"} — 玩家指令 > 自主日常
        # 审批策略 (Codex approval policy 对照): auto / on-failure / never
        # 由环境变量 NPC_APPROVAL_POLICY 配置, 默认 auto(安全)
        import os as _os
        _policy = _os.environ.get("NPC_APPROVAL_POLICY", "auto")
        self.approval_policy = _policy if _policy in APPROVAL_POLICY_VALUES else "auto"
        # 按 NPC 粒度的审批覆盖（Codex /approvals 对照）: {action: allow|ask|deny}
        # 优先级: 本实例覆盖 > 会话级全局覆盖 > 环境变量 > 默认表
        self.approval_overrides: Dict[str, str] = {}
        # 短期对话历史（运行时，不落盘 — 重启清零；只记最近 N 轮，不进记忆卡）
        self.dialogue_history: List[Dict] = []
        # 自定义系统提示词覆盖（高级制作者）— 否则用结构化人格编译的默认模板
        self.system_prompt = self.persona.get("system_prompt_override") or build_system_prompt(self.persona)
        self.use_llm = use_llm
        # LLM 客户端分槽缓存（2026-08-23 修复）: dialogue 槽 = _llm；
        # review 仅在配了不同模型名时才启用独立槽 _llm_review ——
        # 修复前 bug: 单槽使第一个调用者的模型被所有角色共用，NPC_REVIEW_MODEL 永不生效
        self._llm: Optional[LLMClient] = None
        self._llm_review: Optional[LLMClient] = None

    @property
    def actor_pos(self) -> str:
        """此 NPC 在世界中的当前位置（角色槽）。"""
        return self.world["actors"][self.actor_id]["position"]

    def activity_desc(self) -> str:
        """当前日常的展示描述（/api/state 给游戏/前端看）。无活动 → ""。"""
        if self.activity is None:
            return ""
        return f"{self.activity['desc']}（剩 {len(self.activity['steps'])} 步）"

    @staticmethod
    def _read_api_key_file() -> str:
        """直接从项目根 api_key.txt 读 key (不受环境变量污染)。"""
        import pathlib
        f = pathlib.Path(__file__).resolve().parent.parent / "api_key.txt"
        if f.exists():
            k = f.read_text(encoding="utf-8").strip()
            if k:
                return k
        return ""

    def _get_llm(self, role: str = "dialogue") -> Optional[LLMClient]:
        """惰性创建 LLM 客户端（可插拔 — 每角色一个模型，默认云端小模型）。

        role: "dialogue"（B1 对话）/ "review"（A 审查 + 反思归纳）。
        模型名/端点由环境变量配置（NPC_DIALOGUE_MODEL / NPC_REVIEW_MODEL），
        切本地小模型只改环境变量不写代码（见 docs/本地模型.md）。
        无 key 时返回 None → 规则回退。

        v2026-08-23 分槽规则: 两角色配了**不同**模型 → 各自一部电话；
        同款/未配 review → 共用对话槽（单模型场景与旧行为完全一致，
        测试注入 npc._llm 即对全角色生效）。
        """
        if not self.use_llm:
            return None
        import os

        def _model_of(r: str) -> str:
            return {
                "dialogue": os.environ.get("NPC_DIALOGUE_MODEL", "deepseek-v4-flash"),
                "review": os.environ.get("NPC_REVIEW_MODEL", "deepseek-v4-flash"),
            }.get(r, os.environ.get("NPC_DIALOGUE_MODEL", "deepseek-v4-flash"))

        dname = _model_of("dialogue")
        slot, model_name = "_llm", dname
        if role == "review":
            rname = _model_of("review")
            if rname != dname:
                if self._llm_review is not None:
                    return self._llm_review      # 异款已建 → 直接用
                slot, model_name = "_llm_review", rname
            # 同款 → 落到对话槽，与旧版单槽行为一致
        cached = getattr(self, slot)
        if cached is not None:
            return cached
        # key 按模型 provider 匹配对应 env; LLM_API_KEY 是显式通道 key(OpenRouter/自定义/本地);
        # 均未设置则直接读 api_key.txt(绕过被污染的 config)
        lower = model_name.lower()
        env_key = os.environ.get("LLM_API_KEY", "")   # 显式通道优先
        if not env_key:
            if "deepseek" in lower:
                env_key = os.environ.get("DEEPSEEK_API_KEY", "")
            elif "glm" in lower or "zhipu" in lower or "chatglm" in lower:
                env_key = os.environ.get("ZHIPU_API_KEY", "")
            else:
                env_key = os.environ.get("OPENAI_API_KEY", "")
        api_key = env_key or self._read_api_key_file()
        if not api_key:
            log.warning("npc_llm_no_key", npc=self.persona["id"])
            return None
        # base_url: LLM_BASE_URL 显式通道端点优先; 空 → create_provider 按模型名推断(deepseek 等)
        # (不能用 config.BASE_URL 兜底 — 它默认 deepseek,会把 OpenRouter/本地模型发错地方)
        provider = create_provider(
            api_key=api_key, model_name=model_name,
            base_url=os.environ.get("LLM_BASE_URL", ""))
        client = LLMClient(provider=provider)
        setattr(self, slot, client)
        return client

    # ── 记忆卡（设计点 #4: 可编辑文档）────────────────────

    def save(self) -> None:
        """记忆卡落盘: 人格 + 世界状态 + 任务日志 + 笔记。重启后 load() 恢复。"""
        if self.ephemeral:
            return   # 流民不落盘 — despawn 即忘(见 __init__ 注释)
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        card = {
            "id": self.persona["id"],
            "name": self.persona.get("name", ""),
            "saved_at": datetime.now().isoformat(),
            "persona": self.persona,
            "world": self.world,
            "task_log": self.task_log[-50:],   # 只保留最近 50 条（文档可编辑）
            "memory": self.memory.to_dict(),   # 加权记忆（可编辑文档）
            "reflected_upto": self._reflected_upto,   # 反思进度（阶段①）
        }
        self.store_path.write_text(
            json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        log.info("npc_memory_saved", npc=self.persona["id"], path=str(self.store_path))

    @classmethod
    def load(cls, npc_id: str, store_dir: str = "npc/store") -> "NPC":
        """从记忆卡恢复 NPC（重启后记得进度）。"""
        path = Path(store_dir) / f"{npc_id}_memory.json"
        # utf-8-sig: 兼容 Windows 工具（记事本/PowerShell）写入的 BOM — 制作者陷阱防御
        card = json.loads(path.read_text(encoding="utf-8-sig"))
        npc = cls(persona=card["persona"], world=card["world"], store_dir=store_dir)
        npc.task_log = card.get("task_log", [])
        npc.memory.load(card.get("memory", []))
        npc._reflected_upto = card.get("reflected_upto", 0)   # 反思进度（阶段①）
        # 迁移旧格式记忆卡（Day 1 的 "notes" 字段 → 新 memory 格式）
        legacy_notes = card.get("notes", [])
        if legacy_notes and not npc.memory.all():
            for note in legacy_notes:
                npc.memory.add(str(note), importance=5, category="legacy")
        log.info("npc_memory_loaded", npc=npc_id, path=str(path))
        return npc

    def remember(self, note: str, importance: int = 5, category: str = "general") -> None:
        """记一条记忆（追加到记忆卡 — 用户可打开文件直接编辑）。

        importance 0-9: 越高越重要（检索加权 — AI Town 公式）。
        §22 三不清除之"不入记忆卡": 安检门开启时 L1 违规素材在写卡口拒收。
        """
        if _safety.enabled() and _safety.scan(note).level == "L1":
            log.warning("npc_memory_rejected_unsafe", npc=self.persona["id"])
            return
        # 记忆卡治理·闸1(2026-08-25): 同文日常条目(general 且 imp≤6)就地聚合计数,
        # 不再新增重复行 —— 休息循环从 3500 条变 1 条; imp≥7 玩家正事永不合并。
        if _dedup_enabled() and importance <= 6 and category == "general":
            for e in reversed(self.memory.all()):
                if (e.get("category") == "general"
                        and e.get("importance", 5) <= 6
                        and e.get("content") == note):
                    e["created_at"] = time.time()
                    e["count"] = e.get("count", 1) + 1
                    log.info("npc_memory_deduped", npc=self.persona["id"], count=e["count"])
                    return
        self.memory.add(note, importance=importance, category=category)
        log.info("npc_remembered", npc=self.persona["id"], importance=importance)

    def maybe_reflect(self) -> Optional[str]:
        """反思归纳（阶段① 海马体升级）: 重要记忆攒够 → 归纳成高层结论。

        触发: 未反思记忆的重要性之和 ≥ REFLECT_IMPORTANCE_THRESHOLD。
        铁律: 反思只准复述/归纳给定记忆里的事实，禁止编造（防 confabulation）。
        无 LLM → 规则兜底（只做事实摘要，不发明新事实）。
        返回生成的反思文本；未触发 → None。
        """
        entries = self.memory.all()[self._reflected_upto:]
        entries = [e for e in entries if e.get("category") != "reflection"][:REFLECT_MAX_ENTRIES]
        if not entries:
            return None
        if sum(e.get("importance", 5) for e in entries) < REFLECT_IMPORTANCE_THRESHOLD:
            return None
        # 记忆卡治理·闸2(2026-08-25): 候选批唯一内容 <3 → 判定日常噪音,
        # 推进反思指针但不调 LLM、不产废话洞察（防"总结垃圾产生垃圾"）。
        if _dedup_enabled() and len({e.get("content", "") for e in entries}) < 3:
            self._reflected_upto += len(entries)
            log.info("npc_reflect_skipped_noise", npc=self.persona["id"], batch=len(entries))
            return None
        facts = "\n".join(f"- {e['content']}" for e in entries)
        llm = self._get_llm(role="review")     # 反思用 review 档模型（可独立配置）
        if llm is not None:
            reflection = self._reflect_with_llm(llm, facts)
        else:
            reflection = _reflect_rules(entries)
        reflection = reflection.strip() if reflection else ""
        if not reflection:
            return None
        # 闸2b: 产出与既有反思条同文 → 不重写（指针仍前进, 静默翻篇）。
        if _dedup_enabled() and any(
                e.get("category") == "reflection" and e.get("content") == reflection
                for e in self.memory.all()):
            self._reflected_upto = len(self.memory.all())
            log.info("npc_reflect_deduped", npc=self.persona["id"])
            return None
        self.memory.add(reflection, importance=8, category="reflection")
        self._reflected_upto = len(self.memory.all())   # 这批已归纳，不再重复
        self.save()
        log.info("npc_reflected", npc=self.persona["id"])
        return reflection

    def _reflect_with_llm(self, llm, facts: str) -> str:
        """用 LLM 把给定记忆归纳成 1-2 条高层结论（只准基于给定事实）。"""
        prompt = (
            "你是这个角色的自我反思。下面是它最近的真实经历记录（每条都是事实）：\n"
            f"{facts}\n"
            "请归纳出 1-2 条更上层的结论（关于自己/他人/世界的认知），"
            "只能基于上面的事实，禁止编造没出现的细节；每条一句话，用分号隔开。"
        )
        try:
            import os
            _reflect_re = os.environ.get("NPC_REFLECT_REASONING", "max")
            response = SCHED.invoke(P_REFLECT, llm.chat,
                                    [{"role": "user", "content": prompt}],
                                    reasoning_effort=_reflect_re)
            return response.content.strip()
        except Exception as exc:
            log.warning("npc_reflect_llm_fallback", npc=self.persona["id"], error=str(exc))
            return ""

    def consolidate(self) -> int:
        """遗忘合并（阶段③）: 重复主题合并成一条 + 弱旧记忆修剪。返回处理掉的条目数。

        合并/修剪后所有已见条目都已处理 — 反思进度重置到末尾，避免对旧条目重复反思。
        """
        removed = self.memory.consolidate()
        self._reflected_upto = len(self.memory.all())
        if removed:
            self.save()
        return removed

    # ── 感知 ─────────────────────────────────────────

    def observe(self) -> str:
        return observe(self.world, who=self.actor_id)

    # ── 轻路径: 规则快路径任务循环（设计点 B）────────────

    def run_gather_task(self, resource: str, count: int) -> bool:
        """兼容入口: 采集 + 交付（等价于两步任务）。"""
        return self.run_task([
            {"type": "gather", "resource": resource, "count": count},
            {"type": "deliver", "resource": resource, "count": count},
        ])

    def run_task(self, steps: List[Dict]) -> bool:
        """规则循环: 多步任务（gather / craft / deliver），确定性、零 LLM。

        受阻处理（Day 3 实测点）:
          - 资源枯竭 → 找备选地点；无备选 → 优雅失败（有报告，不崩溃）
          - 材料不足/配方缺失/主角不在 → 失败报告
        玩家派活 = 最高优先级（设计点 5: 优先级冲突=用户选）— 直接打断自主日常。
        """
        self.activity = None   # 取消进行中的自主日常
        self.pending_task = None   # 清掉对话下的指令（/api/task 手动派活 > 对话指令）
        self.state = "idle"
        task = {
            "task": " → ".join(f"{s['type']}({s.get('resource') or s.get('recipe')}×{s.get('count', 1)})" for s in steps),
            "started_at": datetime.now().isoformat(),
            "steps": [],
        }
        self.remember(f"接到任务: {task['task']}", importance=6)

        def step(action: str, params: dict) -> bool:
            self.world, ok, msg = apply_action(self.world, action, params, who=self.actor_id)
            task["steps"].append(f"{action}({params}) → {'✓' if ok else '✗'} {msg}")
            log.info("npc_step", npc=self.persona["id"], action=action, ok=ok)
            return ok

        def fail(reason: str) -> bool:
            task["steps"].append(f"失败: {reason}")
            self.task_log.append(task)
            self.save()
            return False

        def walk_to(dest: str) -> bool:
            """沿 BFS 路径走到目的地（地图可能非直连 — Day 3 实测发现）。"""
            path = find_path(self.world, self.actor_pos, dest)
            if path is None:
                return False
            for hop in path:
                if not step("move", {"dest": hop}):
                    return False
            return True

        for spec in steps:
            stype = spec["type"]

            if stype == "gather":
                resource, count = spec["resource"], spec.get("count", 1)
                while self.world["actors"][self.actor_id]["inventory"].get(resource, 0) < count:
                    pos = self.actor_pos
                    if resource in self.world["locations"][pos]["resources"]:
                        if not step("gather", {"resource": resource}):
                            return fail(f"{resource}采尽")
                    else:
                        # 找下一个有该资源的地点（备选）
                        target = next(
                            (n for n, l in self.world["locations"].items()
                             if resource in l["resources"] and l["resources"][resource] > 0),
                            None,
                        )
                        if target is None:
                            return fail(f"世界上没有可采的{resource}")
                        step("say", {"text": f"我去{target}弄点{resource}去。"})
                        if not walk_to(target):
                            return fail(f"无法到达{target}")

            elif stype == "craft":
                recipe, count = spec["recipe"], spec.get("count", 1)
                if self.actor_pos != "村庄":
                    walk_to("村庄")  # 工作台在村庄
                for _ in range(count):
                    if not step("craft", {"recipe": recipe}):
                        return fail(f"制作{recipe}失败")

            elif stype == "deliver":
                resource, count = spec["resource"], spec.get("count", 1)
                prot_pos = self.world["protagonist"]["position"]
                if self.actor_pos != prot_pos:
                    walk_to(prot_pos)
                baseline = self.world["delivered"].get(resource, 0)
                while self.world["delivered"].get(resource, 0) - baseline < count:
                    if not step("deliver", {"resource": resource}):
                        return fail(f"{resource}交付中断")

            else:
                return fail(f"未知步骤类型: {stype}")

        step("say", {"text": f"办妥了！{task['task']}"})
        self.remember(f"完成: {task['task']}", importance=8)
        self.task_log.append(task)
        self.save()
        return True

    # ── 对话（四层架构第 1 层: 玩家交互）────────────────

    def _build_context(self, player_input: str) -> str:
        """构建轻路径上下文: 世界感知 + 加权记忆召回 + 欲望/目标。

        接地指令（可靠性防线）: 要求 NPC 只讲记忆/世界状态里真实存在的事，
        禁止编造没发生过的细节 — Day 2 实测发现 LLM 会即兴发挥（confabulation）。
        """
        mem_block = self.memory.format_for_context(self.memory.retrieve(player_input, top_k=5))
        if _safety.enabled():                 # §22: 记忆段前置红线句（卡片头注）
            mem_block = f"{_safety.REDLINE_LINE}\n{mem_block}"
        parts = [
            "【世界状态】",
            self.observe(),
            "【行为日志】（以下是你自己最近干过的事，不是玩家的）",
            self._behavior_log(),
            "【记忆】",
            mem_block,
            "【规则】回答只能基于上面【世界状态】【行为日志】和【记忆】中的内容，"
            "没在日志/记忆里发生过的事一律不许说；不知道就说不知道。"
            "若玩家问起刚才的对话（我说过什么/聊了什么/我的名字这类），"
            "按对话记录如实回答，不要转移话题。",
        ]
        desires = self.persona.get("desires", {})
        if desires:
            parts.append("【欲望】" + "、".join(desires.keys()))
        goals = self.persona.get("goals", {})
        if goals:
            parts.append("【目标】" + "、".join(f"{k}({v.get('progress', 0)}/{v.get('target', '?')})" for k, v in goals.items()))
        extra = self.persona.get("context_extra", [])
        if extra:
            parts.append("【自定义状态】" + "；".join(extra))
        return "\n".join(parts)

    def _behavior_log(self) -> str:
        """行为日志: 自己最近干过的事（世界日志里的事实源，防 LLM 编造）。

        Day 2 实测: 提示词接地指令只能压制不能根除 confabulation —
        把"事实"从记忆检索换成世界日志直供，LLM 只准复述。
        """
        lines = [ln for ln in self.world.get("log", []) if ln.startswith(self.actor_id + " ")][-6:]
        return "\n".join(f"- {ln}" for ln in lines) if lines else "（最近没干什么）"

    def talk(self, player_input: str, reasoning=None) -> str:
        """玩家交互 — 小模型对话（DeepSeek Flash）+ 规则回退。

        LLM 挂了或无 key 时自动回退规则回复（Day 1 行为保留）。
        回忆类问题走规则快路径（记忆卡逐字回答）— 防 confabulation 的确定性解法。
        对话下指令（"给我两根木材"）走规则快路径接单 — 派活 > 自主日常。

        §22(2026-08-25) 入站安检门: L1 命中 → 零网关罐头拒绝 + 三不清除
        （历史只存占位对，原文永不落盘）；GATE 关闭时与本函数旧行为一致。
        """
        # ── §22 入站安检门（默认 OFF — NPC_SAFETY_GATE 未设时 scan 直通）──
        v = _safety.scan(player_input)
        if v.level == "L1":
            import hashlib
            digest = hashlib.sha1(player_input.encode("utf-8")).hexdigest()[:8]
            log.warning("npc_safety_blocked", npc=self.persona["id"],
                        level=v.level, category=v.category, digest=digest)   # 只记哈希不记原文
            refusal = v.refusal or _safety.refusal()
            self.dialogue_history.extend([          # 三不清除: 占位对替代原文
                {"role": "user", "content": _safety.PLACEHOLDER_USER},
                {"role": "assistant", "content": refusal},
            ])
            del self.dialogue_history[:-DIALOGUE_HISTORY_TURNS * 2]
            self.save()
            return refusal

        recall = self._try_recall(player_input)
        if recall is not None:
            return recall

        command = self._try_task_command(player_input)
        if command is not None:
            return command

        llm = self._get_llm()
        if llm is None:
            return self._talk_rules(player_input)

        sys_content = self.system_prompt + "\n" + self._build_context(player_input)
        if _safety.enabled():                 # §22: 宪法注入 + L2 软旗提示
            const = _safety.constitution_text()
            if const:
                sys_content = const + "\n\n" + sys_content
            if v.level == "L2":
                sys_content += "\n" + _safety.soft_hint(v.category)
        messages = [
            {"role": "system", "content": sys_content},
            *self.dialogue_history,   # 短期对话历史（最近 N 轮，上下文定期重置）
            {"role": "user", "content": player_input},
        ]
        self._last_thinking = ""
        try:
            reply, self._last_thinking = self._chat_with_review(llm, messages, player_input, reasoning)
        except Exception as exc:  # 任何 API 故障 → 规则回退
            log.warning("npc_llm_fallback", npc=self.persona["id"], error=str(exc))
            reply = self._talk_rules(player_input)
            self._last_thinking = ""

        # 对话不自动入记忆 — Day 2 实测: 逐字记录对话会污染检索（编造内容也进卡）。
        # 记忆只由任务事件和显式 remember() 写入（记忆 = 重要的事，不是聊天记录）。
        # 短期历史只留最近 N 轮（内存态），让村民记得"上一条聊了什么"。
        self.dialogue_history.append({"role": "user", "content": player_input})
        self.dialogue_history.append({"role": "assistant", "content": reply})
        del self.dialogue_history[:-DIALOGUE_HISTORY_TURNS * 2]   # 截断: 只留最近 N 轮
        self.save()
        return reply

    def _chat_with_review(self, llm, messages, player_input: str, reasoning=None):
        """B1 生成回复 → B2 落账尝试 → 出站规则核查 → 违诺重生成一次 → 仍违回退规则。

        §22(2026-08-25): A 语义审查退役 —— 分支整体移除，规则层放行即终审；
        `_a_semantic_block` 按"乙案"封存保留（无人调用）。入站安全由安检门负责。
        焊死"口是心非"不变: LLM 答应去办事但任务未落账 → 规则拦截重说。
        返回 (回复文本, 思考内容) — 思考内容供游戏端可视化。
        """
        thinking = ""
        for attempt in range(2):   # 原始 + 1 次重生成（仅由出站违诺触发）
            response = llm.chat(messages, reasoning_effort=reasoning)
            reply = response.content.strip()
            thinking = getattr(response, "reasoning", "") or ""
            self._maybe_book_via_b2(player_input, reply)   # §17: 先给承诺一个落账机会
            if not should_review("dialogue", reply, player_input, self.approval_policy):
                return reply, thinking
            ok, reason = review_dialogue(self, reply, player_input)
            if ok:
                return reply, thinking         # 规则层放行即终审（A 已退役）
            log.warning("npc_review_blocked", npc=self.persona["id"], attempt=attempt, reason=reason)
            messages = [*messages,
                        {"role": "assistant", "content": reply},
                        {"role": "user", "content": REVIEW_RETRY_HINT}]
        return self._talk_rules(player_input), thinking

    def set_approval(self, action: str, decision: str) -> bool:
        """按 NPC 粒度调整审批策略（Codex /approvals 对照）。"""
        return reviewer_set_approval(action, decision, self.approval_overrides)

    def book(self, task: Dict) -> None:
        """落账口 — 唯一允许创建 pending_task 的入口（B2 编译 + A 审查通过后调用）。

        承诺成立 = 这一步被执行，不是嘴上那番话（AI Town / Executive 共识）。
        """
        self.pending_task = task
        self.activity = None          # 打断自主日常，听玩家的
        self.state = "idle"

    def _try_recall(self, player_input: str) -> Optional[str]:
        """回忆快路径: 问过去的事 → 记忆卡逐字回答（不经过 LLM，不会编）。

        Day 2 实测: LLM 对"你之前干了什么"会即兴发挥（修栅栏/码木头堆都是编的）。
        事实回忆是规则能确定性解决的问题 — 交给规则，可靠性由构造保证。
        """
        recall_words = ("记得", "之前", "干了什么", "做过", "昨天", "前天", "帮过我",
                        "干嘛", "忙什么", "忙啥", "做了什么", "在干嘛", "最近在", "这两天")
        if not any(w in player_input for w in recall_words):
            return None
        entries = self.memory.retrieve(player_input, top_k=3)
        if not entries:
            return "我记性不太好，好像没干过什么特别的。"
        items = "\n".join(f"- {e['content']}" for e in entries)
        return f"我想想啊……{items}"

    def _try_task_command(self, player_input: str) -> Optional[str]:
        """对话下指令: "给我2根木材" → B2 编译 → A 审查可行性 → 落账（唯一入口）。

        规则快路径（零 LLM）— 玩家指令 > 自主日常（接单即打断当前活动）。
        承诺成立 = 指令被推进活动队列（book），不是嘴上那番话。
        """
        task = compile_task(player_input)          # B2: 编译任务单
        if task is None:
            return None
        # Approval 三态 (Codex /approvals 对照): deny → 直接拒绝, 不审查不落账
        if approve_action(task["action"], self.approval_policy, self.approval_overrides) == APPROVE_DENY:
            return f"……这个我不能做（{task['action']} 被禁止）。"
        ok, reason = review_task(self, task)       # A: 审查可行性(白名单+分级)
        if not ok:
            return reason                           # 诚实拒绝，不空口答应
        # L3 高影响动作(deliver)在 auto 策略下仍需深度审查 ——
        # 深度审查由对话层的承诺落账保证, 这里标记任务来源为"已审"后落账
        self.book(task)                             # 落账口: 唯一入口
        return f"好，我这就去弄{task['count']}个{task['resource']}给你。"

    # ── §17 子代理: B2 编译 / A 语义审查（LLM 外壳, 规则层护栏不变）──

    def _maybe_book_via_b2(self, player_input: str, reply: str) -> None:
        """B2 编译子代理: 承诺语境 → LLM 编译任务单 → 过审落账。

        触发闸门（成本控制 — 闲聊零调用）:
          - B1 回复承诺/高风险（与 A 审查同一触发信号）, 或
          - 玩家输入像派活但规则词典没接住（长尾意图 — GTA 类纯对话世界的主路径:
            世界没声明资源词典时规则版 compile_task 永远接不了单, 这里补上）
        纪律: 只在 pending_task 为空时编译; 产物必须过与规则路径完全相同的
        三道门（deny 档 / review_task 白名单+可行性 / 落账口 book）——
        LLM 只提议、代码决定执行（防篡改铁律不变）。
        失败语义: 子代理任何失败 → 不落账（承诺成立=已落账, 铁律天然兜底）。
        """
        if self.pending_task is not None:
            return   # 已有账, 不重复编译
        triggered = (should_review("dialogue", reply, player_input, self.approval_policy)
                     or looks_like_intent(player_input))
        if not triggered or not _sub.subagent_enabled("B2"):
            return
        res = _sub.run_subagent(
            self,
            _sub.SubagentSpec(name="b2_compiler", tag="B2",
                              system=_sub.B2_SYSTEM, role="review"),
            brief=_sub.b2_brief(player_input, reply, get_manifest(),
                                list(get_resource_aliases().keys())))
        task = self._task_from_b2(res)
        if task is None:
            return
        if approve_action(task["action"], self.approval_policy,
                          self.approval_overrides) == APPROVE_DENY:
            return   # 被禁动作不落账; 若回复已许诺, review_dialogue 会拦下逼它重说
        ok, _reason = review_task(self, task)
        if ok:
            self.book(task)                         # 落账口: 唯一入口

    @staticmethod
    def _task_from_b2(res) -> Optional[Dict]:
        """B2 结果 → 合法任务单（清单白名单 + 参数收敛）。任何不合规 → None。"""
        if not res.ok or not isinstance(res.data, dict):
            return None
        action = res.data.get("action")
        if not action:
            return None                              # null = 无任务（合法的"没承诺"）
        spec = get_manifest().get(str(action))
        if spec is None:
            return None                              # 清单外动作不认（防篡改第一道）
        task: Dict = {"action": str(action)}
        for p in spec.get("params", []):
            v = res.data.get(p)
            if v is None:
                continue
            if p == "count":
                try:
                    v = max(1, int(v))
                except (TypeError, ValueError):
                    v = 1
            else:
                v = str(v)[:40]
            task[p] = v
        if "count" in spec.get("params", []) and "count" not in task:
            task["count"] = 1                        # 数量缺省 = 1（与规则版一致）
        return task

    def _a_semantic_block(self, player_input: str, reply: str) -> tuple[bool, str]:
        """A 审查子代理: 规则层放行后的语义深审。返回 (是否拦截, 原因)。

        【已退役 · 乙案封存】(§22 · 2026-08-25) 调用点已从 _chat_with_review 移除，
        本方法保留供日后 A/B 对照实验，生产流水线不再经过此处。
        铁律: 失败/超时/schema 不合规 → 放行（绝不卡对话 — 与规则层同款语义:
        硬约束已由规则层兜住, 语义审查是增益不是闸门）。
        """
        if not _sub.subagent_enabled("A"):
            return False, ""
        booked = self.pending_task
        booked_desc = (f"{booked['action']}×{booked.get('count', 1)}"
                       if isinstance(booked, dict) else "")
        res = _sub.run_subagent(
            self,
            _sub.SubagentSpec(name="a_reviewer", tag="A",
                              system=_sub.A_SYSTEM, role="review"),
            brief=_sub.a_brief(str(self.persona.get("identity", "")),
                               list(self.persona.get("taboos", [])),
                               player_input, reply, booked_desc))
        if not res.ok or not isinstance(res.data, dict):
            return False, ""                         # 响亮失败在 stats 里, 放行在行为里
        block = res.data.get("block")
        if not isinstance(block, bool):
            return False, ""
        return block, str(res.data.get("reason", ""))[:80]

    def _talk_rules(self, player_input: str) -> str:
        """规则模式回复 — 从人设 rules 配置读取（制作者自己配，不用写代码）。

        旧记忆卡无人设 rules 字段时自动用默认（向后兼容）。
        """
        rules = self.persona.get("rules", {})
        replies = rules.get("replies", {})
        fallback = rules.get("fallback", "嗯，我在听。")
        for key, reply in replies.items():
            if key in player_input:
                return reply
        return fallback

    def __repr__(self) -> str:
        return f"<NPC {self.persona.get('name', self.persona['id'])} @ {self.world['actors'][self.actor_id]['position']}>"
