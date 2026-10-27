"""V3/V4 基线采集 —— 真实 key 跑一轮受控会话，产出 trace 统计（p50/p95）。

  run    进程内起服务（临时 store，不碰真实数据），按固定脚本打 N 轮 /api/talk，
         覆盖：闲聊 / 带动作 / 带 remember / 带 recall / 混合（同一句里既记又做）；
         NPC_TRACE_FILE 落 trace.jsonl，同目录另写 kinds.json（按回合顺序记 kind）。
  stats  对任意 trace JSONL 算 rounds、first_token_ms、total_ms、tokens.total 的 p50/p95；
         找到 kinds.json（同目录，或 --kinds 指定）时再按 kind 分组输出。
         mixed 组的「动作丢失」和全局 invalid_args 占比是**启发式**口径 —— 看结论前先看标注。

用法：
  python scripts/baseline_trace.py run --out out/baseline_v3
  python scripts/baseline_trace.py run --limit 3        # 只跑前 3 条（冒烟）
  python scripts/baseline_trace.py stats out/baseline_v3/trace.jsonl

环境变量透传（T11：翻默认值前的两遍对照）：
  run 起的是**进程内**服务，进程环境变量直接生效；`--env KEY=VALUE` 可临时设（可重复），
  跑完恢复原值。同一份代码关、开各跑一遍，各自写进自己的 --out：
    python scripts/baseline_trace.py run --out out/base_off --env NPC_SILENT_REMEMBER=0
    python scripts/baseline_trace.py run --out out/base_on  --env NPC_SILENT_REMEMBER=1
  再分别 stats 两个目录里的 trace.jsonl（各自带 kinds.json）。

说明：
  · 复用 scripts/manual_checks.py 的进程内 uvicorn / SSE / mock Mod 助手，口径与手测一致；
  · run 需要真实 key（环境变量或 api_key.txt），每轮都是真实模型调用；
  · trace 只记长度与分层 token（turn.py 的口径），不记台词原文。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import manual_checks as mc                     # noqa: E402  进程内服务 / SSE / mock Mod 助手

# ── 固定脚本：37 轮，类别交错，remember 在前、recall 随后 ──────────────
# kind 只用于人工归类与汇总，不进服务端；混合用例追加在尾部，
# 前面的 32 轮保持原样（与 V3 基线可比）。
SCRIPT: List[Dict[str, str]] = [
    {"kind": "chat", "message": "在吗？"},
    {"kind": "chat", "message": "你是谁呀？简单介绍一下自己。"},
    {"kind": "chat", "message": "今天心情怎么样？"},
    {"kind": "chat", "message": "给我讲个简短的笑话吧。"},
    {"kind": "remember", "message": "记住：我最喜欢吃牛肉面，讨厌香菜。"},
    {"kind": "remember", "message": "记一下：我下周三要交房租。"},
    {"kind": "remember", "message": "记住：我家住在绿荫小区 3 号楼。"},
    {"kind": "remember", "message": "记住：我的生日是 5 月 20 日。"},
    {"kind": "recall", "message": "你还记得我喜欢吃什么吗？"},
    {"kind": "recall", "message": "查查记忆，我下周要做什么来着？"},
    {"kind": "recall", "message": "我住在哪里？你翻一下记忆看看。"},
    {"kind": "recall", "message": "帮我 recall 一下，你都记得关于我的哪些事？"},
    {"kind": "action", "message": "我有点饿了，帮我做碗面。"},
    {"kind": "action", "message": "给我 500 块钱。"},
    {"kind": "action", "message": "带我去公园逛逛。"},
    {"kind": "action", "message": "再做一份蛋炒饭。"},
    {"kind": "chat", "message": "最近怎么样？"},
    {"kind": "remember", "message": "记住：我养了一只叫「汤圆」的猫。"},
    {"kind": "recall", "message": "汤圆是什么？你查下记忆。"},
    {"kind": "action", "message": "帮我做碗面，多放点葱。"},
    {"kind": "action", "message": "给我 100 块。"},
    {"kind": "chat", "message": "我们认识多久了？"},
    {"kind": "remember", "message": "记住：我讨厌下雨天。"},
    {"kind": "recall", "message": "下雨天我心情会不好吗？你还记得吗？"},
    {"kind": "action", "message": "带我去图书馆。"},
    {"kind": "chat", "message": "陪我聊聊天吧。"},
    {"kind": "recall", "message": "我刚才让你做什么了？"},
    {"kind": "remember", "message": "记住：我在学吉他。"},
    {"kind": "action", "message": "做个西红柿炒蛋。"},
    {"kind": "chat", "message": "讲讲你今天都经历了什么。"},
    {"kind": "recall", "message": "我说过我讨厌什么吗？"},
    {"kind": "chat", "message": "明天见，晚安。"},
    # ── 混合用例（T11）：同一句里既要求记住、又要求做个动作 ──
    # 判据：`NPC_SILENT_REMEMBER` 开着时这些回合的动作不能被丢（见第 4 节）
    {"kind": "mixed", "message": "记住我爱吃面，顺便做一碗。"},
    {"kind": "mixed", "message": "记一下我下周三交房租，再给我 100 块。"},
    {"kind": "mixed", "message": "记住我讨厌香菜，给我做份蛋炒饭。"},
    {"kind": "mixed", "message": "记住我明早要早起，带我去图书馆。"},
    {"kind": "mixed", "message": "记住我养了只猫叫汤圆，帮我做碗面多放点葱。"},
]


# ── 统计 ─────────────────────────────────────────────────

def _pct(values: List[float], p: float) -> Optional[float]:
    """最近秩百分位（nearest-rank）。空列表 → None。"""
    ordered = sorted(v for v in values if v is not None)
    if not ordered:
        return None
    rank = max(1, math.ceil(p / 100.0 * len(ordered)))
    return ordered[rank - 1]


def _stats_block(values: List[float]) -> Dict[str, Any]:
    vals = [v for v in values if v is not None]
    return {
        "n": len(vals),
        "p50": _pct(vals, 50),
        "p95": _pct(vals, 95),
        "min": min(vals) if vals else None,
        "max": max(vals) if vals else None,
        "mean": round(sum(vals) / len(vals), 1) if vals else None,
    }


# mixed 组「动作丢失」的判定口径 —— **启发式**：只看该回合 tools 里有没有
# kind=action 的条目，不看模型是否本来就该拒绝。结论要人复核，不能当定论。
MIXED_LOST_NOTE = "启发式：mixed 组里该回合 tools 没有任何 kind=action 的条目"


def _is_action_call(tool: Dict[str, Any]) -> bool:
    """trace 里的一条工具记录是不是动作调用（turn.py 的 kind 口径）。"""
    return str(tool.get("kind") or "") == "action"


def _invalid_args_stats(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """invalid_args 数量与占全部 action 调用的比例（T6 翻默认值要的数据）。"""
    action_calls = 0
    invalid = 0
    for rec in records:
        for tool in rec.get("tools") or []:
            if not _is_action_call(tool):
                continue
            action_calls += 1
            if str(tool.get("status") or "") == "invalid_args":
                invalid += 1
    return {
        "count": invalid,
        "action_calls": action_calls,
        "ratio": round(invalid / action_calls, 4) if action_calls else None,
    }


def _kind_groups(records: List[Dict[str, Any]],
                 kinds: List[str]) -> Dict[str, Dict[str, Any]]:
    """按 kind 分组：回合数 / rounds 与两种耗时的 p50、p95 / ended_early 计数。"""
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for index, rec in enumerate(records):
        kind = kinds[index] if index < len(kinds) else ""
        grouped.setdefault(str(kind).strip() or "unknown", []).append(rec)
    groups: Dict[str, Dict[str, Any]] = {}
    for kind, recs in grouped.items():
        block: Dict[str, Any] = {
            "turns": len(recs),
            "rounds": {"p50": _pct([rec.get("rounds") for rec in recs], 50),
                       "p95": _pct([rec.get("rounds") for rec in recs], 95)},
            "first_token_ms": {"p50": _pct([rec.get("first_token_ms") for rec in recs], 50),
                               "p95": _pct([rec.get("first_token_ms") for rec in recs], 95)},
            "total_ms": {"p50": _pct([rec.get("total_ms") for rec in recs], 50),
                         "p95": _pct([rec.get("total_ms") for rec in recs], 95)},
            "ended_early": sum(1 for rec in recs if rec.get("ended_early")),
        }
        if kind == "mixed":
            block["action_missing"] = sum(
                1 for rec in recs
                if not any(_is_action_call(tool) for tool in (rec.get("tools") or [])))
            block["action_missing_note"] = MIXED_LOST_NOTE
        groups[kind] = block
    return groups


def compute_stats(records: List[Dict[str, Any]],
                  kinds: Optional[List[str]] = None,
                  kinds_source: str = "") -> Dict[str, Any]:
    """trace JSONL 记录 → 各指标 p50/p95 + 工具覆盖汇总（+ 可选按 kind 分组）。

    `kinds` = kinds.json 里按回合顺序的 kind 列表，与 records 一一对应；
    给了才出 `kinds` 分组块，不给则输出与 V3 基线完全一致（只多 invalid_args 一行）。
    """
    tool_names: Dict[str, int] = {}
    for rec in records:
        for tool in rec.get("tools") or []:
            name = tool.get("name", "?")
            tool_names[name] = tool_names.get(name, 0) + 1
    slow_first = [i + 1 for i, rec in enumerate(records)
                  if (rec.get("first_token_ms") or 0) > 15000]
    stats: Dict[str, Any] = {
        "metrics": {
            "rounds": _stats_block([rec.get("rounds") for rec in records]),
            "first_token_ms": _stats_block([rec.get("first_token_ms") for rec in records]),
            "total_ms": _stats_block([rec.get("total_ms") for rec in records]),
            "tokens_total": _stats_block([(rec.get("tokens") or {}).get("total") for rec in records]),
        },
        "tools_called": tool_names,
        "llm_fallback": sum(1 for rec in records if rec.get("llm_fallback")),
        "no_dialogue": sum(1 for rec in records if rec.get("no_dialogue")),
        "summarized": sum(1 for rec in records if rec.get("summarized")),
        "first_token_over_15s": slow_first,   # 疑似冷缓存 / 长思考的回合（1 起）
        "invalid_args": _invalid_args_stats(records),
    }
    if kinds:
        stats["kinds"] = {
            "source": kinds_source or "kinds.json",
            "turns": len(kinds),
            "records": len(records),
            "groups": _kind_groups(records, kinds),
        }
    return stats


def _fmt(block: Dict[str, Any]) -> str:
    return ("n=%s  p50=%s  p95=%s  min=%s  max=%s  mean=%s"
            % (block["n"], block["p50"], block["p95"], block["min"], block["max"], block["mean"]))


# ── 子命令：run ──────────────────────────────────────────

# 跑完记进 stats.json 的开关取值（复盘"这份基线是哪个默认值跑的"）
_ENV_RECORD_KEYS = ("NPC_SILENT_REMEMBER", "NPC_VALIDATE_ACTION_ARGS",
                    "AGENT_MODEL", "NPC_MODEL", "NPC_REASONING_EFFORT")


def _parse_env_pairs(items: List[str]) -> Dict[str, str]:
    """`--env KEY=VALUE`（可重复）→ dict；格式不对响亮退出。"""
    pairs: Dict[str, str] = {}
    for item in items or []:
        if "=" not in item:
            raise SystemExit("--env 需要 KEY=VALUE 形式，收到: %s" % item)
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise SystemExit("--env 的 KEY 不能为空: %s" % item)
        pairs[key] = value.strip()
    return pairs


def _env_snapshot(extra: Dict[str, str]) -> Dict[str, str]:
    """本次运行实际生效的开关取值（没设就是空串）—— 关/开两遍的对照靠它认。"""
    keys = list(_ENV_RECORD_KEYS) + sorted(extra)
    return {key: os.environ.get(key, "") for key in dict.fromkeys(keys)}


def kinds_payload(kinds: List[str]) -> Dict[str, Any]:
    """kinds.json 的内容：按回合顺序的 kind 列表（stats 分组用）。"""
    return {"script_size": len(kinds), "kinds": list(kinds)}


def cmd_run(args: argparse.Namespace) -> int:
    from contextlib import ExitStack

    from core import config
    if not config.api_key():
        print("未检测到真实 key：请配置 NPC_API_KEY（或 api_key.txt），再跑 run。")
        return 2
    extra_env = _parse_env_pairs(getattr(args, "env", None) or [])

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    trace_file = out_dir / "trace.jsonl"
    if trace_file.exists():
        trace_file.unlink()                     # 只统计本次

    script = SCRIPT[:args.limit] if args.limit else list(SCRIPT)
    paths = mc._temp_paths()
    httpd, execute_url = mc.start_mock_mod(delay=0.0)
    app = mc.server.create_app(llm_client=None, paths=paths)
    if app.state.llm_holder["client"] is None:
        print("LLM 三件套未配齐（模型名 / key / 端点），run 需要真实模型。")
        httpd.shutdown()
        return 2

    rounds: List[Dict[str, Any]] = []
    env_record: Dict[str, str] = {}
    # 环境变量透传：TRACE_FILE + --env 给的开关都在本次运行期间生效，跑完恢复原值
    env_ctx = ExitStack()
    env_ctx.enter_context(mc.env_var("NPC_TRACE_FILE", str(trace_file)))
    for key, value in extra_env.items():
        env_ctx.enter_context(mc.env_var(key, value))
    try:
        env_record = _env_snapshot(extra_env)      # 必须趁着还没恢复原值取快照
        print("环境：%s" % json.dumps(env_record, ensure_ascii=False))
        with mc.ServedApp(app) as served:
            base = served.base_url
            mc._post_json(base + "/api/capabilities", {
                "mod": "manual", "execute_url": execute_url,
                "actions": [
                    {"name": "cook", "desc": "做一道菜", "params": {"dish": "菜名"}},
                    {"name": "give_money", "desc": "给玩家钱", "params": {"amount": "金额"}},
                    {"name": "go_to", "desc": "去某个地点", "params": {"place": "地点"}},
                ]})
            t_all = time.perf_counter()
            for i, item in enumerate(script, 1):
                result = mc.stream_talk(base + "/api/talk",
                                        {"npc_id": "cang", "message": item["message"],
                                         "mod": "manual"},
                                        timeout=args.timeout)
                actions = [f["frame"].get("action") for f in result["frames"]
                           if f["frame"].get("type") == "action"]
                rounds.append({
                    "round": i, "kind": item["kind"], "message": item["message"],
                    "first_frame_ms": result["first_frame_ms"], "total_ms": result["end_ms"],
                    "actions": actions, "reply_len": len(result["text"]),
                    "error": result["error"] or None,
                })
                print("第 %2d/%d 轮 [%s] 首帧 %8.1f ms | 总 %8.1f ms | 动作 %d | %s%s"
                      % (i, len(script), item["kind"],
                         result["first_frame_ms"] or -1, result["end_ms"], len(actions),
                         item["message"][:18],
                         (" | 错误: " + result["error"]) if result["error"] else ""))
            print("全部 %d 轮完成，墙钟 %.1f s" % (len(script), time.perf_counter() - t_all))
    finally:
        env_ctx.close()                         # 先恢复环境（后面的统计不该受开关影响）
        httpd.shutdown()

    kinds = [str(item["kind"]) for item in script]      # 按回合顺序，与 trace 一一对应
    records = _read_trace(trace_file)
    stats = compute_stats(records, kinds=kinds,
                          kinds_source=str(out_dir / "kinds.json"))
    stats["env"] = env_record
    (out_dir / "kinds.json").write_text(
        json.dumps(kinds_payload(kinds), ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "rounds.json").write_text(
        json.dumps({"script_size": len(script), "rounds": rounds}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    (out_dir / "stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")

    _print_report(stats, len(records))
    print("输出目录: %s（trace.jsonl / kinds.json / rounds.json / stats.json）" % out_dir)
    return 0 if all(not r["error"] for r in rounds) else 1


# ── 子命令：stats ────────────────────────────────────────

def cmd_stats(args: argparse.Namespace) -> int:
    trace_path = Path(args.trace)
    records = _read_trace(trace_path)
    if not records:
        print("trace 里没有记录：%s" % args.trace)
        return 1
    kinds_path = Path(args.kinds) if args.kinds else trace_path.parent / "kinds.json"
    kinds: Optional[List[str]] = None
    if kinds_path.exists():
        kinds = _read_kinds(kinds_path)
        if kinds is None:
            print("kinds 文件里没有 kind 列表，跳过分组：%s" % kinds_path)
    elif args.kinds:
        print("找不到 kinds 文件：%s" % kinds_path)
        return 1
    stats = compute_stats(records, kinds=kinds,
                          kinds_source=str(kinds_path) if kinds else "")
    if kinds and len(kinds) != len(records):
        print("注意：kinds.json %d 条 / trace %d 条 不对齐 —— 缺的记 unknown，多的 kind 忽略"
              % (len(kinds), len(records)))
    _print_report(stats, len(records))
    if args.out:
        Path(args.out).write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
        print("已写 %s" % args.out)
    return 0


def _read_trace(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    for raw in path.read_text("utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _read_kinds(path: Path) -> Optional[List[str]]:
    """读 kinds.json（`{"kinds": [...]}` 或裸数组）→ 按回合顺序的 kind 列表。

    读不出 / 不是列表 / 空列表 → None（调用方据此保持原有输出）。
    列表里的空项记 unknown，占住位置不挪动对齐。
    """
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    raw = data.get("kinds") if isinstance(data, dict) else data
    if not isinstance(raw, list) or not raw:
        return None
    return [str(item).strip() if str(item).strip() else "unknown" for item in raw]


def _print_report(stats: Dict[str, Any], n: int) -> None:
    m = stats["metrics"]
    print("")
    print("=== V3 基线统计（%d 回合） ===" % n)
    print("rounds        %s" % _fmt(m["rounds"]))
    print("first_token_ms %s" % _fmt(m["first_token_ms"]))
    print("total_ms      %s" % _fmt(m["total_ms"]))
    print("tokens.total  %s" % _fmt(m["tokens_total"]))
    print("工具调用计数   %s" % json.dumps(stats["tools_called"], ensure_ascii=False))
    print("llm_fallback=%s  no_dialogue=%s  summarized=%s"
          % (stats["llm_fallback"], stats["no_dialogue"], stats["summarized"]))
    if stats["first_token_over_15s"]:
        print("首 token >15s 的回合（疑似冷缓存/长思考）: %s" % stats["first_token_over_15s"])
    invalid = stats.get("invalid_args") or {}
    if invalid:
        ratio = ("无 action 调用" if invalid.get("ratio") is None
                 else "%.2f%%" % (invalid["ratio"] * 100))
        print("invalid_args   %d 次 / %d 次 action 调用 = %s"
              % (invalid.get("count", 0), invalid.get("action_calls", 0), ratio))
    env = stats.get("env")
    if env:
        print("env            %s" % json.dumps(env, ensure_ascii=False))
    if stats.get("kinds"):
        _print_kinds(stats["kinds"])


_KIND_ORDER = ("chat", "remember", "recall", "action", "mixed", "unknown")


def _pair(block: Dict[str, Any]) -> str:
    return "p50=%s p95=%s" % (block.get("p50"), block.get("p95"))


def _print_kind_row(kind: str, group: Dict[str, Any]) -> None:
    print("%-9s %4d 回合  rounds[%s]  first_token_ms[%s]  total_ms[%s]  ended_early=%d"
          % (kind, group["turns"], _pair(group["rounds"]),
             _pair(group["first_token_ms"]), _pair(group["total_ms"]),
             group["ended_early"]))


def _print_kinds(block: Dict[str, Any]) -> None:
    """按 kind 分组的小表（含 mixed 的启发式动作丢失、ended_early 计数）。"""
    groups = block.get("groups") or {}
    print("")
    print("=== 按 kind 分组（%s；%d 回合） ===" % (block.get("source"), block.get("turns") or 0))
    for kind in _KIND_ORDER:
        if kind in groups:
            _print_kind_row(kind, groups[kind])
    for kind in sorted(set(groups) - set(_KIND_ORDER)):
        _print_kind_row(kind, groups[kind])
    mixed = groups.get("mixed")
    if mixed:
        print("mixed 动作丢失: %s 条（%s）"
              % (mixed.get("action_missing"), mixed.get("action_missing_note")))


# ── 入口 ─────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="失控NPC v4 V3 基线采集")
    sub = parser.add_subparsers(dest="command", required=True)

    p1 = sub.add_parser("run", help="真实 key 跑固定脚本会话，落 trace 并统计")
    p1.add_argument("--out", default=str(ROOT / "out" / ("baseline_%s" % time.strftime("%Y%m%d_%H%M%S"))))
    p1.add_argument("--limit", type=int, default=0, help="只跑前 N 轮（0 = 全部）")
    p1.add_argument("--timeout", type=float, default=180.0, help="单轮 SSE 读取超时（秒）")
    p1.add_argument("--env", action="append", metavar="KEY=VALUE",
                    help="本次运行临时设的环境变量（可重复），如 NPC_SILENT_REMEMBER=1")
    p1.set_defaults(func=cmd_run)

    p2 = sub.add_parser("stats", help="对 trace JSONL 算 p50/p95（有 kinds.json 时按 kind 分组）")
    p2.add_argument("trace", help="trace JSONL 路径")
    p2.add_argument("--out", default="", help="统计结果另存为 JSON（可选）")
    p2.add_argument("--kinds", default="",
                    help="kinds.json 路径（默认取 trace 同目录的 kinds.json）")
    p2.set_defaults(func=cmd_stats)
    return parser


def main() -> int:
    mc._utf8_stdout()
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
