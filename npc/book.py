"""任务预订三道门（P2 拆分序④·2026-08-25 自 npc.py 合流）。

铁律执行点: LLM 只提议、代码决定执行 —— 规则快路径与 B2 编译路径都必须
经过 guarded_book, 不允许任何旁路。此前 deny→review→book 序列在两条路径
各有一份拷贝, 现收敛为单点(改门只改一处)。

状态语义(供调用方区分玩家话术):
  booked      落账成功(承诺成立 = 已落账)
  denied      审批策略禁止
  unfeasible  可行性审查未过(detail=诚实拒绝语)
  no_consumer 协议模式下无消费者接盘(调用方通常选择诚实沉默)
"""
from __future__ import annotations

from typing import Dict, Tuple

from npc.reviewer import APPROVE_DENY, approve_action, review_task


def guarded_book(npc, task: Dict) -> Tuple[str, str]:
    """三道门顺序: ①审批策略(deny) → ②可行性(review_task) → ③能力协商+落账。

    门3(含账本镜像)在 NPC.book 内部; 任何一道拒绝都不会产生 pending_task。
    返回 (status, detail)。
    """
    action = task.get("action", "")
    if approve_action(action, npc.approval_policy,
                      npc.approval_overrides) == APPROVE_DENY:
        return "denied", f"{action} 被禁止"
    ok, reason = review_task(npc, task)
    if not ok:
        return "unfeasible", reason
    if not npc.book(task):
        return "no_consumer", "无消费者接盘"
    return "booked", ""