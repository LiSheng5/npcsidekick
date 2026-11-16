"""玩家行为库 —— SQLite 存储、合并、清理、推送选取与可见查询。

两个库（投递方式不同，只提供事实资料，不自动替代长期记忆）：
  · npc_interactions —— 玩家**对这个 NPC** 发起的互动；回合开始推送进提示词
    （【玩家对你做了什么】块），只有完整成功的回合才把实际送进提示词的新事
    按装配时保存的 (id, last_ts) 版本化标记已读。
  · player_acts —— 玩家**做了什么**的一般行为；AI 用 check_player_log 工具
    按可见性拉取（public 或自己在 act_witnesses 里，过滤在 SQL 完成，AI 不能全知）。

存储：store/events.db（标准库 sqlite3，不加依赖）。WAL、timeout=5、每连接
PRAGMA foreign_keys=ON；每次调用开关连接，函数签名带 store_dir（None =
工程默认 store/，与 memory.py 同口径）。同步 IO —— server/turn 侧一律
asyncio.to_thread。

块内行文案（[新]/[之前]/相对时间/×n/折叠行/目录行/查询行）集中定义在本模块；
context.py 只负责块标题与预算（保持纯函数）。
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from core import config
from core.logging_config import log
from core.paths import require_npc_id, resolve_store, valid_npc_id

DB_NAME = "events.db"

# ── 校验口径（POST /api/events）────────────────────────────
KIND_TO_NPC = "to_npc"
KIND_BY_PLAYER = "by_player"
_KINDS = (KIND_TO_NPC, KIND_BY_PLAYER)
_VISIBILITIES = ("witnessed", "public")

_TYPE_RE = re.compile(r"^[\w.-]{1,40}$", re.UNICODE)
_MAX_LABEL_CHARS = 8          # label：中文短名不超过 8 个字符
_MAX_SUMMARY_CHARS = 200      # summary：一句话不超过 200 个字符
_MAX_GAME_TIME_CHARS = 64
_MAX_MERGE_KEY_CHARS = 128
_MAX_LOCATION_CHARS = 128
_MAX_DETAIL_BYTES = 4096      # detail：UTF-8 JSON 序列化后不超过 4KB
_MAX_EVENTS_PER_POST = 50

# ── 行文案常量（单一来源）──────────────────────────────────
PREFIX_NEW = "[新]"
PREFIX_RECENT = "[之前]"
NO_RESULT_TEXT = "没有符合条件的记录"
_TRUNCATE_TAIL = "…（已截断）"

# 清理限流：每个库（解析后的绝对路径）最多每分钟清一次
_CLEANUP_INTERVAL_S = 60.0
_cleanup_last: Dict[str, float] = {}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS npc_interactions (
  id TEXT PRIMARY KEY,
  npc_id TEXT NOT NULL,
  ts REAL NOT NULL,
  last_ts REAL NOT NULL,
  game_time TEXT,
  type TEXT NOT NULL,
  label TEXT,
  summary TEXT NOT NULL,
  severity INTEGER NOT NULL DEFAULT 3,
  count INTEGER NOT NULL DEFAULT 1,
  merge_key TEXT,
  detail TEXT,
  delivered_at REAL
);
CREATE INDEX IF NOT EXISTS ix_ni ON npc_interactions(npc_id, last_ts);
CREATE TABLE IF NOT EXISTS player_acts (
  id TEXT PRIMARY KEY, ts REAL NOT NULL, last_ts REAL NOT NULL,
  game_time TEXT, type TEXT NOT NULL, label TEXT, summary TEXT NOT NULL,
  severity INTEGER NOT NULL DEFAULT 3, count INTEGER NOT NULL DEFAULT 1,
  merge_key TEXT, location TEXT, detail TEXT,
  visibility TEXT NOT NULL DEFAULT 'witnessed'
);
CREATE TABLE IF NOT EXISTS act_witnesses (
  act_id TEXT NOT NULL,
  npc_id TEXT NOT NULL,
  PRIMARY KEY (act_id, npc_id),
  FOREIGN KEY (act_id) REFERENCES player_acts(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_pa ON player_acts(last_ts);
"""


def db_path(store_dir: Optional[Path] = None) -> Path:
    """库文件路径：store/events.db（store_dir=None → 工程默认 store/）。"""
    return resolve_store(store_dir) / DB_NAME


def _connect(store_dir: Optional[Path] = None) -> sqlite3.Connection:
    """开一条连接：建目录、建表、WAL、foreign_keys=ON；调用方负责 close。"""
    path = db_path(store_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:                     # 个别文件系统不支持 WAL → 告警后照用默认日志模式
        log.warning("events_wal_unavailable", path=str(path))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    return conn


# ── 相对时间（服务端按真实时钟计算；game_time 仅展示）─────────

def relative_time(ts: float, now: float) -> str:
    """epoch 秒 → 「3分钟前」这类相对时间文本（服务端真实时钟口径）。"""
    secs = max(0, int(now - ts))
    if secs < 60:
        return f"{secs}秒前"
    if secs < 3600:
        return f"{secs // 60}分钟前"
    if secs < 86400:
        return f"{secs // 3600}小时前"
    return f"{secs // 86400}天前"


# ── 校验与归一（纯函数；任何一条非法整批拒绝）──────────────────

def _bad(index: int, field: str, problem: str) -> None:
    raise ValueError(f"events[{index}].{field} {problem}")


def _opt_text(index: int, raw: Dict[str, Any], key: str, max_chars: int) -> Optional[str]:
    """可选字符串字段：缺省/空白 → None；超长/非字符串 → 拒绝。"""
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        _bad(index, key, "必须是字符串")
    value = value.strip()
    if not value:
        return None
    if len(value) > max_chars:
        _bad(index, key, f"超过 {max_chars} 个字符")
    return value


def _validate_one(index: int, raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        _bad(index, "", "必须是对象")
    kind = raw.get("kind")
    if kind not in _KINDS:
        _bad(index, "kind", "必须是 to_npc 或 by_player")

    etype = raw.get("type")
    if not isinstance(etype, str) or not _TYPE_RE.match(etype):
        _bad(index, "type", "必须是 1-40 位字母/数字/下划线/点/横线")

    summary = raw.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        _bad(index, "summary", "必填且不能为空")
    if len(summary) > _MAX_SUMMARY_CHARS:
        _bad(index, "summary", f"超过 {_MAX_SUMMARY_CHARS} 个字符")

    severity = raw.get("severity")
    if severity is None:
        severity = 3
    if isinstance(severity, bool) or not isinstance(severity, int) or not 0 <= severity <= 9:
        _bad(index, "severity", "必须是 0-9 的整数")

    label = raw.get("label")
    if label is not None:
        if not isinstance(label, str) or not label.strip():
            label = None                       # 空白按缺省（展示时回落 type）
        elif len(label) > _MAX_LABEL_CHARS:
            _bad(index, "label", f"超过 {_MAX_LABEL_CHARS} 个字符")

    visibility = raw.get("visibility")
    if visibility is None:
        visibility = "witnessed"
    if visibility not in _VISIBILITIES:
        _bad(index, "visibility", "必须是 witnessed 或 public")

    game_time = _opt_text(index, raw, "game_time", _MAX_GAME_TIME_CHARS)
    merge_key = _opt_text(index, raw, "merge_key", _MAX_MERGE_KEY_CHARS)
    location = _opt_text(index, raw, "location", _MAX_LOCATION_CHARS)

    detail = raw.get("detail")
    detail_json: Optional[str] = None
    if detail is not None:
        if not isinstance(detail, dict):
            _bad(index, "detail", "必须是对象")
        try:                                   # 不得包含无法 JSON 序列化的游戏对象
            detail_json = json.dumps(detail, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            _bad(index, "detail", "无法序列化为 JSON（不得包含游戏对象）")
        if len(detail_json.encode("utf-8")) > _MAX_DETAIL_BYTES:
            _bad(index, "detail", f"序列化后超过 {_MAX_DETAIL_BYTES} 字节")

    npc_id: Optional[str] = None
    witnesses: List[str] = []
    if kind == KIND_TO_NPC:
        npc_id = raw.get("npc_id")
        if not isinstance(npc_id, str) or not valid_npc_id(npc_id):
            _bad(index, "npc_id", "必填且必须是合法 NPC id")
    else:
        raw_witnesses = raw.get("witnesses")
        if raw_witnesses is None:
            raw_witnesses = []
        if not isinstance(raw_witnesses, list):
            _bad(index, "witnesses", "必须是字符串数组")
        for j, w in enumerate(raw_witnesses):
            if not isinstance(w, str) or not valid_npc_id(w):
                _bad(index, f"witnesses[{j}]", "必须是合法 NPC id")
        witnesses = list(dict.fromkeys(raw_witnesses))   # 去重（保序）

    return {
        "kind": kind, "npc_id": npc_id, "type": etype, "label": label,
        "summary": summary.strip(), "severity": severity,
        "visibility": visibility, "game_time": game_time,
        "merge_key": merge_key, "location": location,
        "detail_json": detail_json, "witnesses": witnesses,
    }


def validate_events(raw: Any) -> List[Dict[str, Any]]:
    """POST 载荷里的 events 数组 → 归一化列表；任何一条非法 → ValueError
    （消息指明 `events[i].字段`，server 转 400，整批不落库）。"""
    if not isinstance(raw, list) or not 1 <= len(raw) <= _MAX_EVENTS_PER_POST:
        got = len(raw) if isinstance(raw, list) else "非数组"
        raise ValueError(f"字段 events 必须是 1-{_MAX_EVENTS_PER_POST} 条的数组"
                         f"（收到 {got}）")
    return [_validate_one(i, ev) for i, ev in enumerate(raw)]


# ── 写入（整批单事务；合并规则见任务书 §4）───────────────────

def _mergeable_last_ts(conn: sqlite3.Connection, sql: str, args: Sequence[Any]
                       ) -> Optional[sqlite3.Row]:
    """取同键最新一行（合并候选）；没有 → None。"""
    return conn.execute(sql, args).fetchone()


def _upsert_interaction(conn: sqlite3.Connection, ev: Dict[str, Any],
                        moment: float) -> int:
    """npc_interactions：窗口内同 (npc_id, type, merge_key) 合并（delivered_at 复位）。
    返回是否发生了合并（0/1）。"""
    merge_key = ev["merge_key"] or ""
    row = _mergeable_last_ts(
        conn,
        "SELECT id, last_ts FROM npc_interactions "
        "WHERE npc_id=? AND type=? AND merge_key=? ORDER BY last_ts DESC LIMIT 1",
        (ev["npc_id"], ev["type"], merge_key))
    if row is not None and moment - float(row["last_ts"]) <= config.event_merge_seconds():
        conn.execute(
            "UPDATE npc_interactions SET last_ts=?, game_time=?, label=?, summary=?, "
            "severity=?, count=count+1, detail=?, delivered_at=NULL WHERE id=?",
            (moment, ev["game_time"], ev["label"], ev["summary"], ev["severity"],
             ev["detail_json"], row["id"]))
        return 1
    conn.execute(
        "INSERT INTO npc_interactions (id, npc_id, ts, last_ts, game_time, type, label, "
        "summary, severity, count, merge_key, detail, delivered_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,1,?,?,NULL)",
        (uuid.uuid4().hex, ev["npc_id"], moment, moment, ev["game_time"], ev["type"],
         ev["label"], ev["summary"], ev["severity"], merge_key, ev["detail_json"]))
    return 0


def _upsert_act(conn: sqlite3.Connection, ev: Dict[str, Any], moment: float) -> int:
    """player_acts：窗口内同 (type, merge_key, location) 合并；目击者并集、
    公开性只升不降（新旧任一 public → public）。返回是否发生了合并（0/1）。"""
    merge_key = ev["merge_key"] or ""
    location = ev["location"]
    row = _mergeable_last_ts(
        conn,
        "SELECT id, last_ts, visibility FROM player_acts "
        "WHERE type=? AND merge_key=? AND location IS ? ORDER BY last_ts DESC LIMIT 1",
        (ev["type"], merge_key, location))
    merged = 0
    if row is not None and moment - float(row["last_ts"]) <= config.event_merge_seconds():
        visibility = ("public" if row["visibility"] == "public" or ev["visibility"] == "public"
                      else "witnessed")
        conn.execute(
            "UPDATE player_acts SET last_ts=?, game_time=?, label=?, summary=?, "
            "severity=?, count=count+1, detail=?, visibility=? WHERE id=?",
            (moment, ev["game_time"], ev["label"], ev["summary"], ev["severity"],
             ev["detail_json"], visibility, row["id"]))
        act_id = str(row["id"])
        merged = 1
    else:
        act_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO player_acts (id, ts, last_ts, game_time, type, label, summary, "
            "severity, count, merge_key, location, detail, visibility) "
            "VALUES (?,?,?,?,?,?,?,?,1,?,?,?,?)",
            (act_id, moment, moment, ev["game_time"], ev["type"], ev["label"],
             ev["summary"], ev["severity"], merge_key, location, ev["detail_json"],
             ev["visibility"]))
    for w in ev["witnesses"]:
        conn.execute("INSERT OR IGNORE INTO act_witnesses (act_id, npc_id) VALUES (?,?)",
                     (act_id, w))
    return merged


def record_events(events: List[Dict[str, Any]], store_dir: Optional[Path] = None,
                  now: Optional[float] = None) -> Tuple[int, int]:
    """整批写入（单个事务，半完成状态不外露）→ (accepted, merged)。

    数据库异常 → 整批回滚后原样上抛（调用方回明确 5xx，不得假成功）。
    写入成功后顺手触发限流清理（每库每分钟最多一次）。
    """
    moment = time.time() if now is None else float(now)
    merged = 0
    conn = _connect(store_dir)
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
            for ev in events:
                if ev["kind"] == KIND_TO_NPC:
                    merged += _upsert_interaction(conn, ev, moment)
                else:
                    merged += _upsert_act(conn, ev, moment)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()
    maybe_cleanup(store_dir)
    return len(events), merged


# ── 清理（写入时顺手做；限流状态按库的绝对路径隔离）────────────

def cleanup(store_dir: Optional[Path] = None, now: Optional[float] = None
            ) -> Dict[str, int]:
    """删超过保留天数的记录（severity 不豁免，player_acts 级联删目击者）；
    再对每表执行条数上限（从最旧的 severity<8 开始删，severity>=8 豁免，
    因此高严重度记录较多时允许表暂时超过上限）。返回各步删除计数。"""
    moment = time.time() if now is None else float(now)
    cutoff = moment - config.events_keep_days() * 86400.0
    max_rows = config.events_max()
    deleted: Dict[str, int] = {}
    conn = _connect(store_dir)
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
            deleted["npc_interactions_expired"] = conn.execute(
                "DELETE FROM npc_interactions WHERE last_ts < ?", (cutoff,)).rowcount
            deleted["player_acts_expired"] = conn.execute(
                "DELETE FROM player_acts WHERE last_ts < ?", (cutoff,)).rowcount   # 级联删目击者
            for table in ("npc_interactions", "player_acts"):
                total = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                if total <= max_rows:
                    continue
                deleted[f"{table}_overcap"] = conn.execute(
                    f"DELETE FROM {table} WHERE id IN ("
                    f"SELECT id FROM {table} WHERE severity < 8 "
                    f"ORDER BY last_ts ASC LIMIT ?)", (total - max_rows,)).rowcount
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()
    return deleted


def maybe_cleanup(store_dir: Optional[Path] = None,
                  now: Optional[float] = None) -> bool:
    """写入后顺手清理的限流闸：同一库（解析后的绝对路径）每分钟最多清一次。
    清理失败只告警，不影响写入结果。返回本次是否真的执行了清理。"""
    key = str(db_path(store_dir).resolve())
    moment = time.time() if now is None else float(now)
    last = _cleanup_last.get(key, 0.0)
    if moment - last < _CLEANUP_INTERVAL_S:
        return False
    _cleanup_last[key] = moment              # 先记账：失败的清理也别每笔写入都重试
    try:
        cleanup(store_dir, now=moment)
        return True
    except Exception as exc:                 # 清理失败不连累写入
        log.warning("events_cleanup_failed", path=key, error=type(exc).__name__)
        return False


# ── 推送选取（【玩家对你做了什么】块的行；budget 收敛在 context.py）──

def _push_line(prefix: str, row: sqlite3.Row, moment: float) -> str:
    """行格式：`[新] 3分钟前（16:25:11 day:0 week:1）玩家对你发起了拥抱互动 ×4`；
    没有 game_time 时省略括号；×n 仅 count>1 时出现。"""
    when = relative_time(float(row["last_ts"]), moment)
    game_time = row["game_time"]
    paren = f"（{game_time}）" if game_time else ""
    times = f" ×{row['count']}" if int(row["count"]) > 1 else ""
    return f"{prefix} {when}{paren}{row['summary']}{times}"


def _row(prefix: str, row: sqlite3.Row, moment: float, is_new: bool,
         deliver: List[Tuple[str, float]]) -> Dict[str, Any]:
    return {"text": _push_line(prefix, row, moment), "severity": int(row["severity"]),
            "new": is_new, "last_ts": float(row["last_ts"]), "deliver": deliver}


def select_push(npc_id: str, store_dir: Optional[Path] = None,
                now: Optional[float] = None) -> List[Dict[str, Any]]:
    """该 NPC 的推送行（拼好文本；context.assemble 只管块标题与预算）。

    · 新事：delivered_at IS NULL，severity 降序、last_ts 降序，前
      NPC_EVENT_PUSH_MAX 条逐条展示；超出的折叠成一行「另有 N 条较轻的：…」
      （该行保留时其包含事件才算送达）。
    · 近事：已送达且 last_ts 在 NPC_EVENT_RECENT_MINUTES 内，最多
      NPC_EVENT_RECENT_MAX 条（不参与已读状态变更）。
    行 dict：{text, severity, new, last_ts, deliver}（deliver = 该行保留时
    要标记已读的 (id, last_ts) 列表；近事恒为空）。
    """
    require_npc_id(npc_id)
    moment = time.time() if now is None else float(now)
    conn = _connect(store_dir)
    try:
        new_rows = conn.execute(
            "SELECT id, last_ts, game_time, type, label, summary, severity, count "
            "FROM npc_interactions WHERE npc_id=? AND delivered_at IS NULL "
            "ORDER BY severity DESC, last_ts DESC", (npc_id,)).fetchall()
        recent_cutoff = moment - config.event_recent_minutes() * 60.0
        recent_rows = conn.execute(
            "SELECT id, last_ts, game_time, type, label, summary, severity, count "
            "FROM npc_interactions WHERE npc_id=? AND delivered_at IS NOT NULL "
            "AND last_ts >= ? ORDER BY last_ts DESC LIMIT ?",
            (npc_id, recent_cutoff, config.event_recent_max())).fetchall()
    finally:
        conn.close()

    push_max = config.event_push_max()
    out: List[Dict[str, Any]] = []
    for r in new_rows[:push_max]:
        out.append(_row(PREFIX_NEW, r, moment, True, [(r["id"], float(r["last_ts"]))]))
    overflow = new_rows[push_max:]
    if overflow:
        out.append(_collapse_row(overflow, moment))
    for r in recent_rows:
        out.append(_row(PREFIX_RECENT, r, moment, False, []))
    return out


def _collapse_row(overflow: List[sqlite3.Row], moment: float) -> Dict[str, Any]:
    """超出推送上限的新事折叠行：「另有 N 条较轻的：发起拥抱×3、发生争执×2」。
    N 与各 label 的 ×n 均按发生次数（count 字段）计；该行保留时其包含的全部
    (id, last_ts) 才算送达。"""
    label_counts: Dict[str, int] = {}
    total = 0
    for r in overflow:
        label_counts[r["label"] or r["type"]] = label_counts.get(
            r["label"] or r["type"], 0) + int(r["count"])
        total += int(r["count"])
    parts = "、".join(f"{label}×{n}" for label, n in
                     sorted(label_counts.items(), key=lambda kv: (-kv[1], kv[0])))
    return {"text": f"另有 {total} 条较轻的：{parts}",
            "severity": min(int(r["severity"]) for r in overflow),
            "new": True,
            "last_ts": max(float(r["last_ts"]) for r in overflow),
            "deliver": [(r["id"], float(r["last_ts"])) for r in overflow]}


def mark_delivered(items: Sequence[Tuple[str, float]],
                   store_dir: Optional[Path] = None,
                   now: Optional[float] = None) -> int:
    """版本化已读标记：按装配时保存的 (id, last_ts) 条件更新 delivered_at。

    回合进行中同一事件又发生合并 → last_ts 已变化 → 旧回合的 (id, last_ts)
    不再匹配，新的一次保持未读。返回实际更新的行数。
    """
    moment = time.time() if now is None else float(now)
    conn = _connect(store_dir)
    updated = 0
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
            for event_id, last_ts in items:
                cur = conn.execute(
                    "UPDATE npc_interactions SET delivered_at=? WHERE id=? AND last_ts=?",
                    (moment, str(event_id), float(last_ts)))
                updated += cur.rowcount
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()
    return updated


# ── 可见查询（check_player_log / 目录行；过滤在 SQL 完成）─────

def _visible_clause(npc_id: str) -> Tuple[str, List[Any]]:
    """可见性过滤（SQL 层）：public 或自己在 act_witnesses 里。"""
    return ("(visibility='public' OR id IN "
            "(SELECT act_id FROM act_witnesses WHERE npc_id=?))", [npc_id])


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def visible_acts(npc_id: str, keyword: Optional[str] = None, minutes: Optional[int] = None,
                 limit: int = 8, store_dir: Optional[Path] = None,
                 now: Optional[float] = None) -> Tuple[List[Dict[str, Any]], int]:
    """该 NPC 可见的 player_acts 查询（只读）→ (行列表, 匹配总行数)。

    keyword 匹配 type/label/summary 子串（LIKE 转义）；minutes 只看最近多少
    分钟；按 last_ts 倒序取前 limit 条。可见性过滤在 SQL 里完成。
    """
    require_npc_id(npc_id)
    moment = time.time() if now is None else float(now)
    where, args = _visible_clause(npc_id)
    if keyword:
        pattern = f"%{_escape_like(keyword)}%"
        where += (" AND (type LIKE ? ESCAPE '\\' OR label LIKE ? ESCAPE '\\' "
                  "OR summary LIKE ? ESCAPE '\\')")
        args += [pattern, pattern, pattern]
    if minutes is not None:
        where += " AND last_ts >= ?"
        args.append(moment - float(minutes) * 60.0)
    conn = _connect(store_dir)
    try:
        total = int(conn.execute(
            f"SELECT COUNT(*) FROM player_acts WHERE {where}", args).fetchone()[0])
        rows = conn.execute(
            f"SELECT id, last_ts, type, label, summary, count, location "
            f"FROM player_acts WHERE {where} ORDER BY last_ts DESC LIMIT ?",
            args + [max(1, int(limit))]).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows], total


def format_log_lines(rows: List[Dict[str, Any]], remaining: int = 0,
                     now: Optional[float] = None) -> str:
    """查询行 → 给 LLM 的文本：`[编号6位] 相对时间｜label｜summary ×n｜地点`。
    无地点省略最后一段；超出 limit 加一行「还有 N 条没列出」；无结果给固定文案。"""
    moment = time.time() if now is None else float(now)
    if not rows:
        return NO_RESULT_TEXT
    lines: List[str] = []
    for r in rows:
        label = r.get("label") or r.get("type")
        times = f" ×{r['count']}" if int(r.get("count", 1)) > 1 else ""
        line = (f"[{str(r['id'])[:6]}] {relative_time(float(r['last_ts']), moment)}"
                f"｜{label}｜{r['summary']}{times}")
        if r.get("location"):
            line += f"｜{r['location']}"
        lines.append(line)
    if remaining > 0:
        lines.append(f"还有 {remaining} 条没列出")
    return "\n".join(lines)


# ── 目录行（让 AI 知道有东西可查）──────────────────────────

def digest_info(npc_id: str, store_dir: Optional[Path] = None,
                now: Optional[float] = None) -> Tuple[str, bool]:
    """(目录行文本, 是否有可见记录)，一次查询两用（目录行 + 工具开关）。

    总条数按数据库记录行数计；各 label 的次数按 count 字段计；数量最多的
    前 3 个 label 进文案。窗口 = NPC_PLAYER_LOG_DIGEST_MINUTES；没有记录 →
    ("", False)（目录行整块省略、不带查询工具）。
    """
    require_npc_id(npc_id)
    moment = time.time() if now is None else float(now)
    cutoff = moment - config.player_log_digest_minutes() * 60.0
    where, args = _visible_clause(npc_id)
    conn = _connect(store_dir)
    try:
        rows = conn.execute(
            f"SELECT label, type, count FROM player_acts WHERE last_ts >= ? AND {where}",
            [cutoff] + args).fetchall()
    finally:
        conn.close()
    if not rows:
        return "", False
    label_counts: Dict[str, int] = {}
    for r in rows:
        label_counts[r["label"] or r["type"]] = (
            label_counts.get(r["label"] or r["type"], 0) + int(r["count"]))
    top = sorted(label_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
    parts = "、".join(f"{label}×{n}" for label, n in top)
    hours = config.player_log_digest_minutes() / 60.0
    return (f"你能查到最近 {hours:g} 小时内 {len(rows)} 条玩家行为（{parts}），"
            f"想了解细节可查记录"), True


def has_visible(npc_id: str, store_dir: Optional[Path] = None,
                now: Optional[float] = None) -> bool:
    """该 NPC 在目录行统计窗内是否有可见的玩家行为（决定给不给查询工具）。"""
    return digest_info(npc_id, store_dir=store_dir, now=now)[1]


def digest_line(npc_id: str, store_dir: Optional[Path] = None,
                now: Optional[float] = None) -> str:
    """目录行文本；没有可见记录 → 空串（整块省略）。"""
    return digest_info(npc_id, store_dir=store_dir, now=now)[0]


# ── 调试查询（GET /api/events）────────────────────────────

def list_events(kind: str, npc_id: Optional[str] = None, limit: int = 50,
                store_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """按 last_ts 倒序的原始行（保留全部字段，含 delivered_at 与 JSON 文本 detail）。

    kind=to_npc：npc_id 给了就过滤该 NPC 的互动；kind=by_player：npc_id 给了
    就过滤该 NPC **可见**的行为（便于调试可见性），不给就全量。
    """
    if kind not in _KINDS:
        raise ValueError(f"kind 必须是 {' 或 '.join(_KINDS)}")
    limit = max(1, min(200, int(limit)))
    conn = _connect(store_dir)
    try:
        if kind == KIND_TO_NPC:
            sql = "SELECT * FROM npc_interactions"
            args: List[Any] = []
            if npc_id:
                sql += " WHERE npc_id=?"
                args.append(require_npc_id(npc_id))
        else:
            sql = "SELECT * FROM player_acts"
            args = []
            if npc_id:
                where, vis_args = _visible_clause(require_npc_id(npc_id))
                sql += f" WHERE {where}"
                args += vis_args
        sql += " ORDER BY last_ts DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()
