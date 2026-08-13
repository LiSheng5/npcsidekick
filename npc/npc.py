"""
NPCSidekick — NPC 运行时（轻路径）。

设计点 B（复杂度分级）: 普通 NPC 走轻路径 = 规则快路径 + 小模型对话，
不启动完整 Plan→Execute→Reflect（那留给 v4 帮手 NPC）。

设计点 #4（记忆=可编辑文档）: 记忆卡是 JSON 文件 — 用户/玩家可直接打开修改。
重启后加载记忆卡即恢复（验收点: 重启后记得进度）。
"""
from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from agent.logging_config import log
from agent.llm.client import LLMClient
from agent.providers.factory import create_provider
from npc.memory import NPCMemory
from npc.persona import SAMPLE_NPC, build_system_prompt
from npc.world import apply_action, default_world, find_path, observe

# 对话模型（普通 NPC 用小模型 — 设计点 A 模型路由分层）
DIALOGUE_MODEL = "deepseek-v4-flash"

# 短期对话历史轮数（只记最近 N 轮 — 上下文定期重置；对话不进长期记忆卡防污染，Day 2 铁律）
DIALOGUE_HISTORY_TURNS = 5


class NPC:
    """一个普通 NPC。每个 NPC 持有独立的: 人格 + 世界状态 + 记忆卡。"""

    def __init__(
        self,
        persona: Optional[Dict] = None,
        world: Optional[Dict] = None,
        store_dir: str = "npc/store",
        use_llm: bool = True,
    ):
        self.persona = dict(SAMPLE_NPC if persona is None else persona)
        self.actor_id = self.persona["id"]
        # 共享世界: 传入世界 = 引用（多 NPC 共用一个世界 — AI Town 模式）；
        # 未传入 = 新建独立世界。
        self.world = deepcopy(default_world()) if world is None else world
        from npc.world import actor_of  # 注册角色槽（共享世界时此 NPC 的槽位）

        actor_of(self.world, self.actor_id)
        self.store_path = Path(store_dir) / f"{self.actor_id}_memory.json"
        self.task_log: List[Dict] = []
        self.memory = NPCMemory()          # 加权记忆（AI Town 公式，MIT）
        # 自主循环运行时状态（不落盘 — 重启 = 活动清零回 idle 重新规划，AI Town 同款语义）
        self.state = "idle"                # "idle" | "walking" | "working" | "resting"
        self.activity: Optional[Dict] = None   # {"item", "steps", "desc"} — 进行中的日常
        self._blocked: Dict = {}           # {(action, resource): 冷却到第几个 tick}
        # 对话下的指令（"给我两根木材" → 村民真去干，tick 循环一步步执行）
        self.pending_task: Optional[Dict] = None   # {"action","resource","count"} — 玩家指令 > 自主日常
        # 短期对话历史（运行时，不落盘 — 重启清零；只记最近 N 轮，不进记忆卡）
        self.dialogue_history: List[Dict] = []
        # 自定义系统提示词覆盖（高级制作者）— 否则用结构化人格编译的默认模板
        self.system_prompt = self.persona.get("system_prompt_override") or build_system_prompt(self.persona)
        self.use_llm = use_llm
        self._llm: Optional[LLMClient] = None

    @property
    def actor_pos(self) -> str:
        """此 NPC 在世界中的当前位置（角色槽）。"""
        return self.world["actors"][self.actor_id]["position"]

    def activity_desc(self) -> str:
        """当前日常的展示描述（/api/state 给游戏/前端看）。无活动 → ""。"""
        if self.activity is None:
            return ""
        return f"{self.activity['desc']}（剩 {len(self.activity['steps'])} 步）"

    def _get_llm(self) -> Optional[LLMClient]:
        """惰性创建 LLM 客户端（DeepSeek 小模型）。无 key 时返回 None → 规则回退。"""
        if not self.use_llm:
            return None
        if self._llm is None:
            import os

            import config

            api_key = os.environ.get("DEEPSEEK_API_KEY") or config.API_KEY
            if not api_key:
                log.warning("npc_llm_no_key", npc=self.persona["id"])
                return None
            provider = create_provider(api_key=api_key, model_name=DIALOGUE_MODEL)
            self._llm = LLMClient(provider=provider)
        return self._llm

    # ── 记忆卡（设计点 #4: 可编辑文档）────────────────────

    def save(self) -> None:
        """记忆卡落盘: 人格 + 世界状态 + 任务日志 + 笔记。重启后 load() 恢复。"""
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        card = {
            "id": self.persona["id"],
            "name": self.persona.get("name", ""),
            "saved_at": datetime.now().isoformat(),
            "persona": self.persona,
            "world": self.world,
            "task_log": self.task_log[-50:],   # 只保留最近 50 条（文档可编辑）
            "memory": self.memory.to_dict(),   # 加权记忆（可编辑文档）
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
        """
        self.memory.add(note, importance=importance, category=category)
        log.info("npc_remembered", npc=self.persona["id"], importance=importance)

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
        parts = [
            "【世界状态】",
            self.observe(),
            "【行为日志】（以下是你自己最近干过的事，不是玩家的）",
            self._behavior_log(),
            "【记忆】",
            self.memory.format_for_context(self.memory.retrieve(player_input, top_k=5)),
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

    def talk(self, player_input: str) -> str:
        """玩家交互 — 小模型对话（DeepSeek Flash）+ 规则回退。

        LLM 挂了或无 key 时自动回退规则回复（Day 1 行为保留）。
        回忆类问题走规则快路径（记忆卡逐字回答）— 防 confabulation 的确定性解法。
        对话下指令（"给我两根木材"）走规则快路径接单 — 派活 > 自主日常。
        """
        recall = self._try_recall(player_input)
        if recall is not None:
            return recall

        command = self._try_task_command(player_input)
        if command is not None:
            return command

        llm = self._get_llm()
        if llm is None:
            return self._talk_rules(player_input)

        messages = [
            {"role": "system", "content": self.system_prompt + "\n" + self._build_context(player_input)},
            *self.dialogue_history,   # 短期对话历史（最近 N 轮，上下文定期重置）
            {"role": "user", "content": player_input},
        ]
        try:
            response = llm.chat(messages)
            reply = response.content.strip()
        except Exception as exc:  # 任何 API 故障 → 规则回退
            log.warning("npc_llm_fallback", npc=self.persona["id"], error=str(exc))
            reply = self._talk_rules(player_input)

        # 对话不自动入记忆 — Day 2 实测: 逐字记录对话会污染检索（编造内容也进卡）。
        # 记忆只由任务事件和显式 remember() 写入（记忆 = 重要的事，不是聊天记录）。
        # 短期历史只留最近 N 轮（内存态），让村民记得"上一条聊了什么"。
        self.dialogue_history.append({"role": "user", "content": player_input})
        self.dialogue_history.append({"role": "assistant", "content": reply})
        del self.dialogue_history[:-DIALOGUE_HISTORY_TURNS * 2]   # 截断: 只留最近 N 轮
        self.save()
        return reply

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
        """对话下指令: "给我2根木材/去砍柴" → 接单（挂 pending_task，tick 循环去执行）。

        规则快路径（零 LLM）— 玩家指令 > 自主日常（接单即打断当前活动）。
        只识别"意图词 + 资源词"双命中，避免误接（"我要去散步"不触发）。
        """
        import re

        resources = {
            "木材": ("木材", "木头", "柴", "木", "树"),
            "浆果": ("浆果", "果"),
            "石头": ("石头", "石"),
        }
        intent_words = ("给我", "给", "要", "需要", "帮我", "弄点", "去砍", "去采")
        if not any(w in player_input for w in intent_words):
            return None
        for resource, aliases in resources.items():
            if not any(a in player_input for a in aliases):
                continue
            # 数量: "两根"/"2个" → 数字；默认 1
            count = 1
            m = re.search(r"([0-9]+|[一二两三四五六七八九十]+)\s*个?", player_input)
            if m:
                raw = m.group(1)
                cn = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
                      "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
                count = cn.get(raw, int(raw) if raw.isdigit() else 1)
            # 诚实应答: 全世界采空了就明说,不空口答应（不然接了单却不动,玩家以为坏了）
            from npc.scheduler import resource_site

            if resource_site(self.world, self.actor_pos, resource) is None:
                return f"……{resource}现在弄不到了，采空了，等它长回来吧。"
            self.pending_task = {"action": "gather", "resource": resource, "count": max(1, count)}
            self.activity = None          # 打断自主日常，听玩家的
            self.state = "idle"
            return f"好，我这就去弄{count}个{resource}给你。"
        return None

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
