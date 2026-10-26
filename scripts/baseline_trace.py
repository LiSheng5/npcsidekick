"""V3 基线采集 —— 真实 key 跑一轮受控会话，产出 trace 统计（p50/p95）。

  run    进程内起服务（临时 store，不碰真实数据），按固定脚本打 N 轮 /api/talk，
         覆盖：闲聊 / 带动作 / 带 remember / 带 recall；NPC_TRACE_FILE 落 JSONL。
  stats  对任意 trace JSONL 算 rounds、first_token_ms、total_ms、tokens.total 的 p50/p95。

用法：
  python scripts/baseline_trace.py run --out out/baseline_v3
  python scripts/baseline_trace.py run --limit 3        # 只跑前 3 条（冒烟）
  python scripts/baseline_trace.py stats out/baseline_v3/trace.jsonl

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

# ── 固定脚本：32 轮，类别交错，remember 在前、recall 随后 ──────────────
# kind 只用于人工归类与汇总，不进服务端。
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


def compute_stats(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """trace JSONL 记录 → 四个指标的 p50/p95 + 工具覆盖汇总。"""
    tool_names: Dict[str, int] = {}
    per_kind: Dict[str, int] = {}
    for rec in records:
        for tool in rec.get("tools") or []:
            name = tool.get("name", "?")
            tool_names[name] = tool_names.get(name, 0) + 1
    slow_first = [i + 1 for i, rec in enumerate(records)
                  if (rec.get("first_token_ms") or 0) > 15000]
    return {
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
    }


def _fmt(block: Dict[str, Any]) -> str:
    return ("n=%s  p50=%s  p95=%s  min=%s  max=%s  mean=%s"
            % (block["n"], block["p50"], block["p95"], block["min"], block["max"], block["mean"]))


# ── 子命令：run ──────────────────────────────────────────

def cmd_run(args: argparse.Namespace) -> int:
    from core import config
    if not config.api_key():
        print("未检测到真实 key：请配置 NPC_API_KEY（或 api_key.txt），再跑 run。")
        return 2

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
    with mc.env_var("NPC_TRACE_FILE", str(trace_file)):
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
    httpd.shutdown()

    records = _read_trace(trace_file)
    stats = compute_stats(records)
    (out_dir / "rounds.json").write_text(
        json.dumps({"script_size": len(script), "rounds": rounds}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    (out_dir / "stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")

    _print_report(stats, len(records))
    print("输出目录: %s（trace.jsonl / rounds.json / stats.json）" % out_dir)
    return 0 if all(not r["error"] for r in rounds) else 1


# ── 子命令：stats ────────────────────────────────────────

def cmd_stats(args: argparse.Namespace) -> int:
    records = _read_trace(Path(args.trace))
    if not records:
        print("trace 里没有记录：%s" % args.trace)
        return 1
    stats = compute_stats(records)
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


# ── 入口 ─────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="失控NPC v4 V3 基线采集")
    sub = parser.add_subparsers(dest="command", required=True)

    p1 = sub.add_parser("run", help="真实 key 跑固定脚本会话，落 trace 并统计")
    p1.add_argument("--out", default=str(ROOT / "out" / ("baseline_%s" % time.strftime("%Y%m%d_%H%M%S"))))
    p1.add_argument("--limit", type=int, default=0, help="只跑前 N 轮（0 = 全部）")
    p1.add_argument("--timeout", type=float, default=180.0, help="单轮 SSE 读取超时（秒）")
    p1.set_defaults(func=cmd_run)

    p2 = sub.add_parser("stats", help="对 trace JSONL 算 p50/p95")
    p2.add_argument("trace", help="trace JSONL 路径")
    p2.add_argument("--out", default="", help="统计结果另存为 JSON（可选）")
    p2.set_defaults(func=cmd_stats)
    return parser


def main() -> int:
    mc._utf8_stdout()
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
