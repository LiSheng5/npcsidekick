"""记忆卡生命周期（P2 拆分序⑥·2026-08-25 自 npc.py R3+R4 迁出）。

save/load 落盘恢复、remember 写入口(安检拒收+闸1聚合)、反思归纳(阈值触发+
闸2噪音跳过+闸2b产出去重)、consolidate 遗忘合并。NPC 经 MemoryCardMixin 继承。
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from agent.config_flags import env_flag
from agent.logging_config import log
from npc import safety as _safety
from npc.scheduler import P_REFLECT, SCHED

# ── 反思归纳参数（阶段① 海马体升级, Generative Agents 同款语义）──
REFLECT_IMPORTANCE_THRESHOLD = 12   # 未反思记忆重要性之和达阈值 → 触发
REFLECT_MAX_ENTRIES = 8             # 一次反思最多纳入的条目数（防上下文过长）


def _reflect_rules(entries: List[Dict]) -> str:
    """规则兜底反思: 只做事实摘要，不发明新事实（防 confabulation）。"""
    tops = sorted(entries, key=lambda e: e.get("importance", 0), reverse=True)[:2]
    return "我最近做了这些事：" + "；".join(t["content"] for t in tops)


class MemoryCardMixin:
    """记忆卡持久化 + 写入治理 + 反思归纳 + 遗忘合并（混入 NPC）。"""

    def save(self) -> None:
        """记忆卡落盘: 人格 + 世界状态 + 任务日志 + 笔记。重启后 load() 恢复。

        P2⑥ 防抖(2026-08-25): 序列化结果与上次完全一致 → 跳过磁盘写。
        高频对话尾部的 save() 不再为"没变化的世界快照"付整卡 IO。
        """
        if self.ephemeral:
            return   # 流民不落盘 — despawn 即忘(见 __init__ 注释)
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
        blob = json.dumps(card, ensure_ascii=False, indent=2)
        if getattr(self, "_last_saved_blob", None) == blob:
            return   # 内容未变 — 免一次整卡 IO
        self._last_saved_blob = blob
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self.store_path.write_text(blob, encoding="utf-8")
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
        if env_flag("NPC_MEMORY_DEDUP") and importance <= 6 and category == "general":
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
        if env_flag("NPC_MEMORY_DEDUP") and len({e.get("content", "") for e in entries}) < 3:
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
        if env_flag("NPC_MEMORY_DEDUP") and any(
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