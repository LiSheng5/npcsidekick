"""记忆卡生命周期（P2 拆分序⑥·2026-08-25 自 npc.py R3+R4 迁出）。

save/load 落盘恢复、remember 写入口(安检拒收+闸1聚合)、反思归纳(阈值触发+
闸2噪音跳过+闸2b产出去重)、consolidate 遗忘合并。NPC 经 MemoryCardMixin 继承。
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from agent.config_flags import env_flag
from agent.logging_config import log
from npc import safety as _safety
from npc.memory import (CATEGORY_ARCHIVED, EV_DONE, EV_FAIL, MTYPES,
                        MTYPE_DEFAULT, _canonical_terms)
from npc.scheduler import P_REFLECT, SCHED

# ── 反思归纳参数（阶段① 海马体升级, Generative Agents 同款语义）──
REFLECT_IMPORTANCE_THRESHOLD = 18   # 未反思记忆重要性之和达阈值 → 触发（2026-08-28 12→18: 闲聊不凑数, 攒够大事才总结）
REFLECT_MAX_ENTRIES = 8             # 一次反思最多纳入的条目数（防上下文过长）

# ── TDAM 借鉴①(2026-08-26): 三分类反思参数 ──
TYPED_REFLECT_MAX = 3               # 一次三分类反思最多产出条数（防爆炸）

# ── TDAM 借鉴②(2026-08-26): NPC 画像层参数 ──
PERSONA_MAX_CHARS = 2000            # 画像文件上限（TDAM persona.md 同款约束）
PERSONA_MATERIAL_ENTRIES = 30       # 画像素材窗口（最近 N 条记忆）


def _reflect_rules(entries: List[Dict]) -> str:
    """规则兜底反思: 只做事实摘要，不发明新事实（防 confabulation）。"""
    tops = sorted(entries, key=lambda e: e.get("importance", 0), reverse=True)[:2]
    return "我最近做了这些事：" + "；".join(t["content"] for t in tops)


def _parse_typed_reflection(raw: str) -> Optional[List[Dict]]:
    """解析三分类反思输出 → [{"mtype","content","importance"}]; 不合法 → None。

    容错链(TDAM l1-parser 同款思路): 剥 Markdown 围栏 → 截取首尾 [] 之间 →
    json.loads → 逐条校验(mtype 白名单外归 episodic; importance 夹取 0-9;
    空 content 丢弃; 上限 TYPED_REFLECT_MAX 条)。任何一步失败整体返回 None,
    调用方无痕落回旧单条反思路径 —— 优雅降级由构造保证。
    """
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None
    out: List[Dict] = []
    for item in data[:TYPED_REFLECT_MAX]:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content", "")).strip()
        if not content:
            continue
        mtype = str(item.get("mtype", "")).strip().lower()
        if mtype not in MTYPES:
            mtype = MTYPE_DEFAULT
        try:
            imp = int(item.get("importance", 5))
        except (TypeError, ValueError):
            imp = 5
        out.append({"mtype": mtype, "content": content,
                    "importance": max(0, min(9, imp))})
    return out or None


def _persona_rules(entries: List[Dict]) -> str:
    """画像规则兜底: 只做统计与摘录, 不发明（TDAM 禁过度推测原则的无 LLM 版）。"""
    freq: dict = {}
    for e in entries:
        for term in _canonical_terms(e.get("content", "")):
            freq[term] = freq.get(term, 0) + 1
    tops = sorted(freq.items(), key=lambda kv: -kv[1])[:3]
    lines = ["# 对玩家的了解（规则版·待 LLM 精修）", "", "## 高频话题"]
    if tops:
        lines += [f"- {name}×{n}" for name, n in tops]
    else:
        lines.append("- （素材不足）")
    refl = [e for e in entries
            if e.get("category") == "reflection" or e.get("mtype") == "persona"][-5:]
    if refl:
        lines += ["", "## 已沉淀的认知"]
        lines += [f"- {e['content']}" for e in refl]
    return "\n".join(lines)


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

    def remember(self, note: str, importance: int = 5, category: str = "general",
                 mtype: str = "") -> None:
        """记一条记忆（追加到记忆卡 — 用户可打开文件直接编辑）。

        importance 0-9: 越高越重要（检索加权 — AI Town 公式）。
        §22 三不清除之"不入记忆卡": 安检门开启时 L1 违规素材在写卡口拒收。
        TDAM 借鉴①(NPC_MEMORY_TYPED=1): EV_DONE/EV_FAIL 任务事件卡确定性
        归为 episodic —— 前缀即客观事件的构造性证据, 不劳 LLM; 开关关零差异。
        """
        if _safety.enabled() and _safety.scan(note).level == "L1":
            log.warning("npc_memory_rejected_unsafe", npc=self.persona["id"])
            return
        if env_flag("NPC_MEMORY_TYPED") and not mtype and (
                note.startswith(EV_DONE) or note.startswith(EV_FAIL)):
            mtype = MTYPE_DEFAULT
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
        self.memory.add(note, importance=importance, category=category, mtype=mtype)
        log.info("npc_remembered", npc=self.persona["id"], importance=importance)

    def maybe_reflect(self) -> Optional[str]:
        """反思归纳（阶段① 海马体升级）: 重要记忆攒够 → 归纳成高层结论。

        触发: 未反思记忆的重要性之和 ≥ REFLECT_IMPORTANCE_THRESHOLD。
        铁律: 反思只准复述/归纳给定记忆里的事实，禁止编造（防 confabulation）。
        无 LLM → 规则兜底（只做事实摘要，不发明新事实）。
        返回生成的反思文本；未触发 → None。
        """
        entries = self.memory.all()[self._reflected_upto:]
        # 任务书#06: archived(管家降级层)不进反思候选 — 已降级的流水账不得借
        # 反思重新归纳成 reflection 还魂（只排除, 绝不删除, 证据链仍在卡上）。
        # 过滤后下面重要性求和与指针推进都跟着这批走（推进量 = 过滤后批长,
        # 别只滤不推 → 见风险表"同一批反复检"）。
        entries = [e for e in entries
                   if e.get("category") not in ("reflection", CATEGORY_ARCHIVED)
                   ][:REFLECT_MAX_ENTRIES]
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
        # ── TDAM 借鉴①(NPC_MEMORY_TYPED=1): 三分类结构化反思 ──
        # LLM 把本批事实提炼成 ≤3 条 persona/episodic/instruction 记忆;
        # 解析失败/LLM 异常 → 返回 None 无痕落回下方旧单条路径（优雅降级）。
        if llm is not None and env_flag("NPC_MEMORY_TYPED"):
            typed = self._reflect_entries_typed(llm, facts)
            if typed is not None:
                existing = {e.get("content") for e in self.memory.all()
                            if e.get("category") == "reflection"}
                fresh = [t for t in typed if t["content"] not in existing]
                if not fresh:
                    # 闸2b 同款语义: 产出全部撞车 → 静默翻篇不重写
                    self._reflected_upto = len(self.memory.all())
                    log.info("npc_reflect_deduped", npc=self.persona["id"])
                    return None
                for t in fresh:
                    self.memory.add(t["content"], importance=t["importance"],
                                    category="reflection", mtype=t["mtype"])
                self._reflected_upto = len(self.memory.all())
                self.save()
                log.info("npc_reflected_typed", npc=self.persona["id"], n=len(fresh))
                return "\n".join(t["content"] for t in fresh)
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

    def _reflect_entries_typed(self, llm, facts: str) -> Optional[List[Dict]]:
        """TDAM 借鉴①: 三分类结构化反思(LLM)。解析失败 → None → 调用方落回旧路径。"""
        prompt = (
            "你是这个角色的自我反思。下面是它最近的真实经历记录（每条都是事实）：\n"
            f"{facts}\n"
            "请提炼成最多 3 条结构化记忆，每条必须归入三类之一：\n"
            "- persona：关于玩家或自己的稳定特质/偏好/习惯\n"
            "- episodic：客观发生的事件（做了什么/去了哪/结果如何）\n"
            "- instruction：玩家提出的长期要求或规矩\n"
            "规则：只能基于上面的事实，禁止编造；每条一句话、独立完整；"
            "宁缺毋滥，琐碎的不提；importance 为 0-9 整数"
            "（核心正事 8-9，一般事件 5-7，琐碎 ≤4）。\n"
            '只输出 JSON 数组，格式：'
            '[{"mtype": "episodic", "content": "...", "importance": 6}]\n'
            "不要输出 Markdown 代码块或其他任何文字。"
        )
        try:
            _reflect_re = os.environ.get("NPC_REFLECT_REASONING", "max")
            response = SCHED.invoke(P_REFLECT, llm.chat,
                                    [{"role": "user", "content": prompt}],
                                    reasoning_effort=_reflect_re)
            parsed = _parse_typed_reflection(response.content)
            if parsed is None:
                log.warning("npc_reflect_typed_unparsed", npc=self.persona["id"])
            return parsed
        except Exception as exc:
            log.warning("npc_reflect_llm_fallback", npc=self.persona["id"], error=str(exc))
            return None

    # ── TDAM 借鉴②(2026-08-26): NPC 画像层(NPC_PERSONA=1 时启用) ──
    # 对玩家的 ≤2000 字画像文件(npc/store/{id}_persona.md, 整目录已 gitignore)。
    # 四层扫描: 基础锚点/兴趣图谱/交互协议/认知内核 —— 渐进式披露的高层结构,
    # 低层原始记忆卡永远保留(证据链不可逆)。

    def persona_path(self):
        """画像文件路径: 与记忆卡同目录。"""
        return self.store_path.parent / f"{self.actor_id}_persona.md"

    def read_persona_profile(self) -> str:
        """读画像(talk 注入口)。开关关/文件缺 → ""（没有就不注入, 零开销）。"""
        if not env_flag("NPC_PERSONA"):
            return ""
        path = self.persona_path()
        try:
            mt = path.stat().st_mtime
        except OSError:
            return ""
        cache = getattr(self, "_persona_cache", ("", 0.0))
        if cache[1] == mt:
            return cache[0]
        try:
            text = path.read_text(encoding="utf-8-sig").strip()
        except Exception:
            text = ""
        self._persona_cache = (text, mt)
        return text

    def _persona_materials(self) -> str:
        """画像素材: 最近 N 条记忆(带类型标注), 只复述不加工。"""
        lines = []
        for e in self.memory.all()[-PERSONA_MATERIAL_ENTRIES:]:
            tag = e.get("mtype") or e.get("category") or ""
            lines.append(f"- [{tag}] {e['content']}" if tag else f"- {e['content']}")
        return "\n".join(lines) or "（暂无记忆素材）"

    def update_persona_profile(self) -> None:
        """四层扫描增量更新对玩家画像。consolidate 低频顺路调用; 开关关零开销直通。

        无 LLM → 规则兜底(只统计与摘录); LLM 输出超长 → 截断到 PERSONA_MAX_CHARS;
        与旧文件全同 → 不落盘。任何文件故障只告警不抛(画像永远不值得炸主循环)。
        """
        if not env_flag("NPC_PERSONA"):
            return
        path = self.persona_path()
        existing = ""
        try:
            if path.exists():
                existing = path.read_text(encoding="utf-8-sig")
        except Exception:
            existing = ""
        materials = self._persona_materials()
        text = ""
        llm = self._get_llm(role="review")
        if llm is not None:
            text = self._persona_with_llm(llm, existing, materials)
        if not text:
            text = _persona_rules(self.memory.all()[-PERSONA_MATERIAL_ENTRIES:])
        text = text.strip()[:PERSONA_MAX_CHARS]
        if not text or text == existing.strip():
            return   # 空产出或无变化 → 免一次 IO
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            self._persona_cache = (text, path.stat().st_mtime)
            log.info("npc_persona_updated", npc=self.actor_id, chars=len(text))
        except Exception as exc:
            log.warning("npc_persona_write_failed", npc=self.actor_id, error=str(exc))

    def _persona_with_llm(self, llm, existing: str, materials: str) -> str:
        """LLM 四层扫描增量修订画像; 失败返回 "" → 调用方落规则兜底。"""
        name = self.persona.get("name", self.actor_id)
        prompt = (
            f"你是角色「{name}」的记忆整理者。请根据真实记忆素材，"
            "维护一份它对玩家的了解画像（markdown，不超过 2000 字）。\n"
            "按四层扫描组织（素材没提到的层可以不写）：\n"
            "## 基础锚点（玩家是谁：称呼/身份/常去的地方）\n"
            "## 兴趣图谱（玩家常做什么、投入最多的事）\n"
            "## 交互协议（玩家说话的习惯、喜欢的回应方式、雷区）\n"
            "## 认知内核（玩家的目标与动机）\n"
            "规则：内容只能来自素材，禁止编造；有旧画像时做增量修订"
            "（保留仍成立的，修正过时的）；只输出画像 markdown 本体。\n"
            f"【旧画像】：\n{existing or '（还没有）'}\n"
            f"【新近记忆素材】：\n{materials}"
        )
        try:
            response = SCHED.invoke(P_REFLECT, llm.chat,
                                    [{"role": "user", "content": prompt}],
                                    reasoning_effort=os.environ.get(
                                        "NPC_REFLECT_REASONING", "max"))
            return response.content.strip()
        except Exception as exc:
            log.warning("npc_persona_llm_fallback", npc=self.actor_id, error=str(exc))
            return ""

    def consolidate(self) -> int:
        """遗忘合并（阶段③）: 重复主题合并成一条 + 弱旧记忆修剪。返回处理掉的条目数。

        合并/修剪后所有已见条目都已处理 — 反思进度重置到末尾，避免对旧条目重复反思。
        2026-08-28: 画像是 LLM 慢变层, 不再与 consolidate(60 tick 零 LLM 巡逻)顺路绑定 —
        触发点移到黎明(一日一次)由 server 驱动; 本方法保持纯规则零 LLM。
        """
        removed = self.memory.consolidate()
        self._reflected_upto = len(self.memory.all())
        if removed:
            self.save()
        return removed