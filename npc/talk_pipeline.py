"""对话管线 B1（P2 拆分序⑤·2026-08-25 自 npc.py R6 迁出）。

TalkPipelineMixin 承载玩家交互四层: 安检门→回忆快路径→指令快路径→LLM+出站审查。
方法体自 NPC 原样迁入(经 self 访问宿主记忆/世界/人设); NPC 继承本混入,
外部调用方(npc.talk / server / 测试)零改动。
"""
from __future__ import annotations

from typing import Optional

from agent.logging_config import log
from npc import safety as _safety
from npc import taskloop as _taskloop
from npc.reviewer import REVIEW_RETRY_HINT, review_dialogue, should_review
from npc.world import (LOG_TIER_AUTONOMOUS, LOG_TIER_INTERACTIVE,
                       log_tier, summarize_autonomous)

# 短期对话历史轮数（只记最近 N 轮 — 上下文定期重置；对话不进长期记忆卡防污染，Day 2 铁律）
DIALOGUE_HISTORY_TURNS = 5


class TalkPipelineMixin:
    """B1 对话管线（混入 NPC）。"""

    def _build_context(self, player_input: str) -> str:
        """构建轻路径上下文: 世界感知 + 加权记忆召回 + 欲望/目标。

        接地指令（可靠性防线）: 要求 NPC 只讲记忆/世界状态里真实存在的事，
        禁止编造没发生过的细节 — Day 2 实测发现 LLM 会即兴发挥（confabulation）。
        """
        mem_block = self.memory.format_for_context(self.memory.retrieve(player_input, top_k=5))
        if _safety.enabled():                 # §22: 记忆段前置红线句（卡片头注）
            mem_block = f"{_safety.REDLINE_LINE}\n{mem_block}"
        # TDAM 借鉴②(2026-08-26, NPC_PERSONA=1): 高层画像渐进式披露 ——
        # 画像段排在原始记忆卡之前, 细节仍由【记忆】按需召回; 开关关 → ""零差异。
        try:
            _profile = self.read_persona_profile()
        except Exception:
            _profile = ""
        parts = [
            "【世界状态】",
            self.observe(),
            "【行为日志】（以下是你自己最近干过的事，不是玩家的）",
            self._behavior_log(),
        ]
        if _profile:
            parts += ["【你对玩家的了解】", _profile]
        parts += [
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
        2026-08-28 日志分档(读侧, 用户 D1 保守裁决): 🌟互动逐条(最近至多 6 条,
        尾向上扫满 6 即停) + 🌗自主活动语言化摘要一行 —— NPC 被问"刚才在干嘛/
        采了多少"照样有整一手事实, 且比连读 6 条流水省 token 且更人话。
        """
        recent: list = []
        got_interactive = 0
        for ln in reversed(self.world.get("log", [])):
            if not ln.startswith(self.actor_id + " "):
                continue
            recent.append(ln)
            if log_tier(ln) == LOG_TIER_INTERACTIVE:
                got_interactive += 1
                if got_interactive >= 6:
                    break
            elif len(recent) >= 120:        # 互动少时兜底窗口(≈6 分钟行为)
                break
        recent.reverse()
        parts = [f"- {ln}" for ln in recent if log_tier(ln) == LOG_TIER_INTERACTIVE]
        auton = summarize_autonomous([ln for ln in recent
                                      if log_tier(ln) == LOG_TIER_AUTONOMOUS])
        if auton:
            parts.append(f"- （自主活动）{auton}")
        return "\n".join(parts) if parts else "（最近没干什么）"

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
            # 2026-08-27 重构（用户拍板）: 罐头拒绝退役 → LLM 以人设口吻婉拒。
            # 安全靠三层: 模型自对齐 + 宪法红线 + hard_hint; 玩家不再"被消失"。
            import hashlib
            digest = hashlib.sha1(player_input.encode("utf-8")).hexdigest()[:8]
            log.warning("npc_safety_blocked", npc=self.persona["id"],
                        level=v.level, category=v.category, digest=digest)   # 只记哈希不记原文
            # 三不清除原理不变: 本函数全程只见占位符（下方 safe_input 一致替换）
            safe_input = _safety.PLACEHOLDER_USER
        else:
            safe_input = player_input

        if v.level != "L1":        # 违规轮跳过快路径 — 不许引用记忆卡/接单/回忆
            recall = self._try_recall(player_input)
            if recall is not None:
                return recall

            command = self._try_task_command(player_input)
            if command is not None:
                return command

        llm = self._get_llm()
        if llm is None:
            # 规则模式**故意不记历史**（见 tests/test_npc.py::test_rules_mode_no_history）:
            # 确定性答复本就无状态，把一串雷同兜底话塞进历史只会污染上下文。
            # 2026-09-08 曾误判此处为 bug 并改成记历史，触发该测试失败后回滚。
            # 副作用可接受: 违规轮在规则模式下同样不入历史 —— 没存即没泄漏，
            # "三不清除"的安全属性依然成立。
            return self._talk_rules(safe_input)   # L1 时 = 占位符（兜底话术不触原文）

        sys_content = self.system_prompt + "\n" + self._build_context(player_input)
        if _safety.enabled():                 # §22: 宪法注入 + L1/L2 提示
            const = _safety.constitution_text()
            if const:
                sys_content = const + "\n\n" + sys_content
            if v.level == "L1":
                sys_content += "\n" + _safety.hard_hint(v.category)   # 模型人设口吻婉拒
            elif v.level == "L2":
                sys_content += "\n" + _safety.soft_hint(v.category)
        # P1-3 商议接线(2026-08-25): 失败任务的"找玩家商量"进入对话上下文 ——
        # pop 即消费(防队列无界增长); 字幕提醒此前已在 task_done 推送过,
        # 这里保证"玩家来了, NPC 嘴上也主动认账"(记忆卡兜底逐字召回)。
        try:
            _disc = _taskloop.LEDGER.pop_discussions(self.actor_id)
            if _disc:
                sys_content += "\n【未完成的事·主动提起】" + "；".join(
                    d["text"] for d in _disc)
        except Exception:
            pass
        messages = [
            {"role": "system", "content": sys_content},
            *self.dialogue_history,   # 短期对话历史（最近 N 轮，上下文定期重置）
            {"role": "user", "content": safe_input},   # L1 时 = 占位符（原文不进上下文）
        ]
        self._last_thinking = ""
        try:
            reply, self._last_thinking = self._chat_with_review(llm, messages, safe_input, reasoning)
        except Exception as exc:  # 任何 API 故障 → 规则回退
            log.warning("npc_llm_fallback", npc=self.persona["id"], error=str(exc))
            reply = self._talk_rules(safe_input)
            self._last_thinking = ""

        # 对话不自动入记忆 — Day 2 实测: 逐字记录对话会污染检索（编造内容也进卡）。
        # 记忆只由任务事件和显式 remember() 写入（记忆 = 重要的事，不是聊天记录）。
        # 短期历史只留最近 N 轮（内存态），让村民记得"上一条聊了什么"。
        # 注意: 仅 LLM 分支记历史；规则模式（无 key / API 故障 early return）不记，
        # 见上方 llm is None 分支的注释。
        self.dialogue_history.append({"role": "user", "content": safe_input})
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