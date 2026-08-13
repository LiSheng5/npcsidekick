"""
上下文压缩器 — 对话历史过长时，将旧消息压缩为要点存入长时记忆。
"""
from __future__ import annotations

import config

COMPRESSION_SYSTEM_PROMPT = """你是一个对话总结助手。将以下对话压缩为关键要点。

规则:
- 每行一个要点，以 "- " 开头
- 提取: 用户偏好、做出的决策、学到的重要信息、用户明确说的事实
- 每句话控制在 15 字以内
- 不要添加评论性内容
- 只输出要点列表，不要其他文字"""


class ContextCompressor:
    """用 LLM 压缩旧的对话轮次。"""

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

        # ── 格式化为对话记录 ──────────────────────────
        transcript_lines = []
        for msg in old_messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            # 截断过长的消息
            if len(content) > 300:
                content = content[:300] + "..."
            transcript_lines.append(f"[{role}] {content}")
        transcript = "\n".join(transcript_lines)

        # ── 调用 LLM 压缩 ─────────────────────────────
        try:
            response = self.llm.chat(
                [
                    {"role": "system", "content": COMPRESSION_SYSTEM_PROMPT},
                    {"role": "user", "content": f"请总结以下对话:\n\n{transcript}"},
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

        # ── 从短时记忆中移除 ──────────────────────────
        if saved_count > 0 or split_idx > 0:
            self.memory.short_term.trim_oldest(split_idx)

        return split_idx
