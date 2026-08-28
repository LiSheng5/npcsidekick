"""事件冷层归档子系统（P2 拆分序②·2026-08-25 自 server.py S6 迁出）。

§18 契约: world.log 内存只留尾部(NPC_LOG_TAIL), 头部自动搬盘 jsonl;
游标 = 绝对流位置(offset+物理索引), 跨轮转恒有效; 归档段由 events_since 回放。
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from agent.logging_config import log
from npc.world import LOG_TAIL_DEFAULT, parse_archive_record, rotate_world_log


def log_tail_cfg() -> int:
    """NPC_LOG_TAIL 环境变量（0=关闭轮转）; 非法值回退默认。每次现读可热切。"""
    try:
        return max(0, int(os.environ.get("NPC_LOG_TAIL", str(LOG_TAIL_DEFAULT))))
    except ValueError:
        return LOG_TAIL_DEFAULT


def archive_dir() -> str:
    return os.environ.get("NPC_LOG_ARCHIVE_DIR", "npc/store/log_archive")


def parse_log_line(line: str) -> Optional[Dict]:
    """把一条 world.log 文本解析成结构化事件。解析不到 → None。"""
    m = re.match(r"^(\S+)\s+说:\s*(.+)$", line)          # "cang 说: 你好"
    if m:
        return {"type": "say", "npc": m.group(1), "text": m.group(2)}
    m = re.match(r"^(\S+)\s+前往\s+(.+)$", line)          # "cang 前往 森林"
    if m:
        return {"type": "move", "npc": m.group(1), "dest": m.group(2)}
    m = re.match(r"^(\S+)\s+(接下|完成)任务[:：]\s*(.+)$", line)  # 任务书#02 首派/销账
    if m:
        return {"type": "task", "npc": m.group(1),
                "status": "started" if m.group(2) == "接下" else "done",
                "desc": m.group(3)}
    m = re.match(r"^(\S+)\s+采集了\s+1\s+个(.+)$", line)  # "cang 采集了 1 个木材"
    if m:
        return {"type": "gather", "npc": m.group(1), "resource": m.group(2)}
    m = re.match(r"^(\S+)\s+制作了\s+(.+)$", line)          # "cang 制作了 木石工具"
    if m:
        return {"type": "craft", "npc": m.group(1), "product": m.group(2)}
    m = re.match(r"^(\S+)\s+将\s+(.+?)\s+交给了\s+(.+)$", line)  # "cang 将 木材 交给了 主角"
    if m:
        return {"type": "deliver", "npc": m.group(1), "resource": m.group(2), "to": m.group(3)}
    m = re.match(r"^\[(B2|A)\]\s+(\S+)\s+(.+)$", line)   # "[B2] cang b2_compiler ✓ ..."（§17）
    if m:
        return {"type": "subagent", "agent": m.group(1).lower(),
                "npc": m.group(2), "text": m.group(3)}
    return None


def archived_events(world, since: int, offset: int) -> List[Dict]:
    """冷回放: since < offset 的段落从归档 jsonl 读回（游标契约跨轮转不破）。"""
    path = Path(archive_dir()) / "log_archive.jsonl"
    if not path.exists():
        return []
    out: List[Dict] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for ln in fh:
                rec = parse_archive_record(ln)
                if rec is not None and since <= rec["i"] < offset:
                    ev = parse_log_line(rec["text"])
                    if ev is not None:
                        out.append(ev)
    except Exception as exc:
        log.warning("log_archive_read_failed", error=str(exc))
    return out


def events_since(first, since: int) -> Tuple[List[Dict], int]:
    """先冷层轮转（超阈值搬头部进归档），再做偏移感知的增量收集。

    返回 (events[], log_count) — log_count = offset + len(log) 是绝对流位置,
    客户端游标语义与旧版完全一致。
    """
    tail = log_tail_cfg()
    if tail > 0:
        try:
            rotate_world_log(first.world, tail=tail, archive_dir=archive_dir())
        except Exception as exc:
            # 写失败 → 放弃本轮轮转, 内存照旧增长 — 绝不因归档丢事件
            log.warning("world_log_rotate_failed", error=str(exc))
    log_list = first.world["log"]
    offset = int(first.world.get("_log_offset", 0))
    total = offset + len(log_list)
    since = max(0, min(since, total))   # 游标边界: 负数/超界都收拢到合法区间
    events: List[Dict] = []
    if since < offset:
        events.extend(archived_events(first.world, since, offset))
    for line in log_list[max(0, since - offset):]:
        ev = parse_log_line(line)
        if ev is not None:
            events.append(ev)
    return events, total