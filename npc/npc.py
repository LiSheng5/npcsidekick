"""
NPCSidekick — NPC 运行时（轻路径）。

设计点 B（复杂度分级）: 普通 NPC 走轻路径 = 规则快路径 + 小模型对话，
不启动完整 Plan→Execute→Reflect（那留给 v4 帮手 NPC）。

设计点 #4（记忆=可编辑文档）: 记忆卡是 JSON 文件 — 用户/玩家可直接打开修改。
重启后加载记忆卡即恢复（验收点: 重启后记得进度）。
"""
from __future__ import annotations

import os
import time
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from agent.logging_config import log
from agent.llm.client import LLMClient

from npc import safety as _safety
from npc import subagent as _sub
from npc import taskloop as _taskloop
from npc import llm_wiring
from npc import book as _booking
from npc.talk_pipeline import DIALOGUE_HISTORY_TURNS, TalkPipelineMixin
from npc.memory_card import MemoryCardMixin, _reflect_rules  # 兼容旧测试导入
from agent.config_flags import env_flag
from npc.memory import EV_DONE, EV_FAIL, NPCMemory
from npc.persona import SAMPLE_NPC, build_system_prompt
from npc.reviewer import (REVIEW_RETRY_HINT, APPROVAL_POLICY_VALUES, compile_task,
                          get_manifest, get_resource_aliases,
                          looks_like_intent, review_dialogue,
                          set_approval as reviewer_set_approval, should_review)
from npc.world import apply_action, default_world, find_path, observe

# §22(2026-08-25): A 审查退役 —— 旧开关读到即警告忽略（机器按"乙案"封存于 subagent.py / _a_semantic_block）
import os as _env_probe
if _env_probe.environ.get("NPC_SUBAGENT_A"):
    log.warning("npc_subagent_a_deprecated",
                hint="A 已退役(§22)，NPC_SUBAGENT_A 被忽略；见 docs/NPC大脑架构.md §22")

# (P2⑤: DIALOGUE_HISTORY_TURNS 迁 npc/talk_pipeline.py, 本模块经导入使用)

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
    return env_flag("NPC_MEMORY_DEDUP")


def memory_dedup_enabled() -> bool:
    """公共只读口(P1-1 观测用)。"""
    return _dedup_enabled()


def _reflect_rules(entries: List[Dict]) -> str:
    """规则兜底反思: 只做事实摘要，不发明新事实（防 confabulation）。"""
    tops = sorted(entries, key=lambda e: e.get("importance", 0), reverse=True)[:2]
    return "我最近做了这些事：" + "；".join(t["content"] for t in tops)


class NPC(TalkPipelineMixin, MemoryCardMixin):
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
        # 温层向量锚点: 持久目录随记忆卡走(NPC_VECTOR_ANCHOR=1 时启用语义检索)
        self.memory = NPCMemory(
            anchor_dir=str(Path(store_dir) / "vectors" / self.actor_id)
        )          # 加权记忆（AI Town 公式，MIT）
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

    def _get_llm(self, role: str = "dialogue") -> Optional[LLMClient]:
        """惰性创建 LLM 客户端（可插拔 — 每角色一个模型，默认云端小模型）。

        role: "dialogue"（B1 对话）/ "review"（审查 + 反思归纳）。
        模型名/端点由环境变量配置（NPC_DIALOGUE_MODEL / NPC_REVIEW_MODEL），
        切本地小模型只改环境变量不写代码（见 docs/本地模型.md）。
        无 key 时返回 None → 规则回退。

        v2026-08-23 分槽规则: 两角色配了**不同**模型 → 各自一部电话；
        同款/未配 review → 共用对话槽（单模型场景与旧行为完全一致，
        测试注入 npc._llm 即对全角色生效）。
        P2 拆分序①(2026-08-25): 装配细节(key 解析/provider 工厂)迁
        npc/llm_wiring.py —— 本方法只保留槽位缓存职责。
        """
        if not self.use_llm:
            return None
        dname = llm_wiring.model_of("dialogue")
        slot, model_name = "_llm", dname
        if role == "review":
            rname = llm_wiring.model_of("review")
            if rname != dname:
                if self._llm_review is not None:
                    return self._llm_review      # 异款已建 → 直接用
                slot, model_name = "_llm_review", rname
            # 同款 → 落到对话槽，与旧版单槽行为一致
        cached = getattr(self, slot)
        if cached is not None:
            return cached
        api_key = llm_wiring.resolve_api_key(model_name)
        if not api_key:
            log.warning("npc_llm_no_key", npc=self.persona["id"])
            return None
        client = llm_wiring.build_client(
            api_key=api_key, model_name=model_name,
            base_url=os.environ.get("LLM_BASE_URL", ""))
        setattr(self, slot, client)
        return client



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
        self.remember(f"{EV_DONE}{task['task']}", importance=8)
        self.task_log.append(task)
        self.save()
        return True

    def set_approval(self, action: str, decision: str) -> bool:
        """按 NPC 粒度调整审批策略（Codex /approvals 对照）。"""
        return reviewer_set_approval(action, decision, self.approval_overrides)

    def book(self, task: Dict) -> bool:
        """落账口 — 唯一允许创建 pending_task 的入口（B2 编译 + A 审查通过后调用）。

        承诺成立 = 这一步被执行，不是嘴上那番话（AI Town / Executive 共识）。
        协议 v1(M1·2026-08-25): NPC_TASK_LOOP=1 时落账前过能力协商门 ——
        动词 ∈ manifest ∩ 活跃消费者能力才接单；没人接盘的活不落账(返回 False)，
        回复层的承诺↔账本一致性核对(review_dialogue)会逼出诚实改口，空头支票发不出去。
        同步镜像进任务账本(task_id/链式/派发销账)；门/账本任何故障降级旧语义不卡对话。
        """
        gate_open = _taskloop.gate_enabled()
        if gate_open:
            try:
                _manifest_actions = set((get_manifest() or {}).keys())
                if not _taskloop.action_allowed(str(task.get("action", "")),
                                                _manifest_actions):
                    log.info("npc_book_rejected_no_consumer",
                             npc=self.actor_id, action=str(task.get("action", "")))
                    return False
            except Exception as exc:
                log.warning("npc_capability_gate_error", error=str(exc))   # 门坏→放行旧语义
        # 提交段两分支共用(消除 P2④ 指出的三连赋值重复)
        self.pending_task = task
        self.activity = None          # 打断自主日常，听玩家的
        self.state = "idle"
        if gate_open:
            try:
                _taskloop.LEDGER.book(self.actor_id, str(task.get("action", "")),
                                      params={k: v for k, v in task.items()
                                              if k != "action"},
                                      desc=str(task.get("task") or task.get("action", "")))
            except Exception as exc:
                log.warning("npc_ledger_book_failed", npc=self.actor_id, error=str(exc))
        return True

    def _try_task_command(self, player_input: str) -> Optional[str]:
        """对话下指令: "给我2根木材" → B2 编译 → A 审查可行性 → 落账（唯一入口）。

        规则快路径（零 LLM）— 玩家指令 > 自主日常（接单即打断当前活动）。
        承诺成立 = 指令被推进活动队列（book），不是嘴上那番话。
        """
        task = compile_task(player_input)          # B2: 编译任务单
        if task is None:
            return None
        # P2④(2026-08-25): 三道门序列已合流 npc/book.py guarded_book —— 单点维护
        status, reason = _booking.guarded_book(self, task)
        if status == "denied":
            return f"……这个我不能做（{reason}）。"
        if status == "unfeasible":
            return reason                           # 诚实拒绝，不空口答应
        if status == "no_consumer":
            return None   # 协议v1: 无消费者接盘 → 不承诺, 对话自然继续(诚实沉默)
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
        status, _reason = _booking.guarded_book(self, task)
        if status != "booked":
            return   # 被禁/不可行/无人接盘均不落账; 若回复已许诺, review_dialogue 会拦下逼它重说

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

    def __repr__(self) -> str:
        return f"<NPC {self.persona.get('name', self.persona['id'])} @ {self.world['actors'][self.actor_id]['position']}>"
