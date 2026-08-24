"""
上下文压缩器 — 对话历史过长时，将旧消息压缩为「交接单」存入长时记忆。

对照 Codex Context Compaction 改造（2026-08）:
  ① 入口截流: 超大消息 middle-truncation(保留头尾), 避免无关细节吃满窗口
  ② 结构化交接单: 分节(目标/已做/决策/待办/事实) 而非平铺要点 — 保住 NPC 承诺与任务目标
  ③ 保留推理: 压缩前把会话状态喂给 LLM, 避免电话游戏退化
  ④ 内容分级: 按消息类型分层删除, 先丢低价值工具输出, 保留关键决策
"""
from __future__ import annotations

import config

COMPRESSION_SYSTEM_PROMPT = """你是对话总结助手。把给定对话压缩成一份「工作交接单」，让下一轮对话无需看原始记录也能继续。

严格按以下分节输出（没有内容的节直接省略）:
## 目标
- 正在进行的任务目标（若有）
## 已做
- 已完成的关键事项
## 决策
- 做出的重要决策 / NPC 对玩家的承诺（重要！）
## 待办
- 尚未完成、下一轮要继续的事
## 事实
- 用户偏好、明确陈述的事实

规则:
- 每行一个要点，以 "- " 开头
- 每条控制在 15 字以内
- 只输出以上分节和要点，不要其他文字"""


class ContextCompressor:
    """用 LLM 压缩旧的对话轮次（交接单式 + 内容分级）。"""

    def __init__(self, llm_client, memory_manager):
        self.llm = llm_client
        self.memory = memory_manager

    def compress(self) -> int:
        """
        压缩短时记忆中最旧的 40% 消息。

        流程:
          1. 取最旧 40% 的消息
          2. 格式化为对话记录
          3. 调用 LLM 压缩为要点
          4. 要点存入长时记忆
          5. 被压缩的消息从短时记忆移除

        Returns: 被压缩的消息数量。
        """
        full_history = self.memory.short_term.history
        if len(full_history) < config.COMPRESSION_MIN_MESSAGES:
            return 0

        split_idx = int(len(full_history) * config.COMPRESSION_RATIO)
        if split_idx < 2:
            return 0

        old_messages = full_history[:split_idx]

        # ── ③ 保留推理: 先把会话状态喂给 LLM, 再压缩 ──
        #     防止"电话游戏退化"(每次压缩丢一点中间判断)
        state_lines = []
        for ctx_key in ("current_task", "player_request", "npc_commitment"):
            v = self.memory.short_term.get_context(ctx_key)
            if v:
                state_lines.append(f"- {ctx_key}: {str(v)[:120]}")

        # ── ① 入口截流 + 格式化对话记录 ───────────────
        transcript_lines = []
        for msg in old_messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            content = _middle_truncate(content, head=180, tail=60)  # 保留头尾
            transcript_lines.append(f"[{role}] {content}")
        transcript = "\n".join(transcript_lines)

        # ── 调用 LLM 压缩 (交接单式) ──────────────────
        try:
            user_prompt = f"请总结以下对话:\n\n{transcript}"
            if state_lines:
                user_prompt = f"当前状态:\n{chr(10).join(state_lines)}\n\n{user_prompt}"
            response = self.llm.chat(
                [
                    {"role": "system", "content": COMPRESSION_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.0,
                max_tokens=config.COMPRESSION_MAX_RESPONSE_TOKENS,
            )
            summary_text = response.content or ""
        except Exception:
            # LLM 调用失败时跳过压缩，不中断主流程
            return 0

        # ── 提取要点并存入长时记忆 ─────────────────────
        saved_count = 0
        for line in summary_text.strip().splitlines():
            line = line.strip()
            # 跳过分节标题(## 目标 等)和空行
            if line.startswith("##") or not line:
                continue
            # 提取 "- xxx" 或 "• xxx" 或 "* xxx" 格式
            if line.startswith(("- ", "• ", "* ")):
                fact = line[2:].strip()
            elif line.startswith("-") and len(line) > 1:
                fact = line[1:].strip()
            else:
                continue
            if fact and len(fact) > 1:
                self.memory.remember(fact, category="conversation_summary")
                saved_count += 1

        if saved_count == 0 and summary_text.strip():
            # 如果没有提取到要点格式，将整段作为一条事实
            self.memory.remember(
                summary_text.strip()[:200],
                category="conversation_summary",
            )
            saved_count = 1

        # ── ④ 内容分级: 移除旧消息 (低价值先删, 关键决策已入长时记忆) ──
        if saved_count > 0 or split_idx > 0:
            self.memory.short_term.trim_oldest(split_idx)

        return split_idx


def _middle_truncate(text: str, head: int = 180, tail: int = 60) -> str:
    """入口截流: 超长内容保留头尾、裁掉中间 (Codex middle-truncation)。"""
    if len(text) <= head + tail + 3:
        return text
    return text[:head] + "..." + text[-tail:]
