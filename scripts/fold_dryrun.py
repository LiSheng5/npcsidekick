"""老存档折叠干跑（T13）—— 不联网、不动原存档，看积压会被怎么切、切成几片。

做什么：
  · 把 `--store` 整份复制到临时目录（原目录只读，一个字节都不改）；
  · 用桩 LLM（固定返回一段文本，不发任何请求）调 `chatlog.maybe_summarize`
    —— `<npc>_chat.jsonl` 原样不动，只有临时副本里的 `_summary.json` 会变；
  · 打印积压轮数、分片数、每片输入 token 与「窗口 × 0.3」的比较，
    以及最终 covered 是否追平 cutoff。

为什么要有它：
  老存档积压很多时，折叠是「按轮次切片、滚动前进」的 —— 切错一片就可能整晚跑不完，
  或者单片撑爆上下文。先干跑一遍再让人决定要不要开压缩。

用法：
  python scripts/fold_dryrun.py --store store --npc cang
  python scripts/fold_dryrun.py --store store --npc cang --window 8000

说明：
  · `--window` 只改临时副本的上下文窗口（写进副本的 llm_config.json，页面层优先），
    不给就用现有设置；
  · 干跑改的是**临时副本**，真实存档不受影响；
  · 退出码：0 = 折完了且 covered 追平 cutoff（或本来就没积压）；
             1 = 有积压但没折完（看输出里的分片与 covered）；
             2 = 参数/存档问题（目录不存在、没有聊天记录等）。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import chatlog                              # noqa: E402
from core.tokens import estimate_tokens     # noqa: E402

# 桩 LLM 的固定回复：折叠只认 resp.content，内容本身不影响统计
_STUB_SUMMARY = "（干跑摘要）玩家与 NPC 已经聊过若干轮，其间的事实略。"


class StubLLM:
    """桩 LLM —— 不联网，回固定文本，顺手记下每次调用的输入 token。

    签名与真实 client 对齐（`chat(messages, reasoning_effort=None, **kw)`），
    所以 `chatlog.maybe_summarize` 不需要任何开关就能用它。
    """

    model_name = "dryrun-stub"

    def __init__(self, reply: str = _STUB_SUMMARY) -> None:
        self.reply = reply
        self.calls: List[Dict[str, Any]] = []

    def chat(self, messages: List[Dict[str, Any]], reasoning_effort: Optional[str] = None,
             **kwargs: Any) -> Any:
        text = "\n".join(str(m.get("content") or "") for m in messages)
        self.calls.append({
            "input_tokens": estimate_tokens(text),
            "messages": len(messages),
            "reasoning_effort": reasoning_effort,
        })
        return SimpleNamespace(content=self.reply)

    def stream(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("干跑只走非流式 chat，不走 stream")


def _apply_window(store_dir: Path, window: int) -> None:
    """`--window` 写进临时副本的 llm_config.json（合并，不覆盖别的字段）。

    必须写文件而不是设环境变量：`llm_settings` 的优先级是 页面 > 文件 > 环境变量，
    副本里若已有 llm_config.json，环境变量层会被文件盖掉。
    """
    path = store_dir / "llm_config.json"
    data: Dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            loaded = {}
        if isinstance(loaded, dict):
            data = loaded
    data["context_window"] = int(window)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def run(store: Path, npc_id: str, window: Optional[int] = None) -> Dict[str, Any]:
    """在存档的临时副本上干跑一次折叠 → 统计字典（原目录只读）。

    `npc_id` 非法 / 存档不存在 / 没有聊天记录 → 抛 ValueError / FileNotFoundError，
    由命令行入口翻译成退出码 2。
    """
    store = Path(store)
    if not store.is_dir():
        raise FileNotFoundError("存档目录不存在: %s" % store)
    chat_file = chatlog.chat_path(npc_id, store)          # 顺带做 npc_id 合法性校验
    if not chat_file.exists():
        raise FileNotFoundError("没有这个 NPC 的聊天记录: %s" % chat_file)

    temp_root = Path(tempfile.mkdtemp(prefix="npc_fold_dryrun_"))
    try:
        temp_store = temp_root / "store"
        shutil.copytree(store, temp_store)                # 原存档只读
        if window:
            _apply_window(temp_store, window)

        # 折叠计划（纯读）：covered 之前已摘要，cutoff 之后是保留的最近几轮
        plan = chatlog._fold_plan(npc_id, None, None, temp_store)
        window_tokens = chatlog.context_window(temp_store)
        slice_cap = max(1, int(window_tokens * chatlog._FOLD_SLICE_RATIO))

        result: Dict[str, Any] = {
            "npc_id": npc_id,
            "store": str(store),
            "window": window_tokens,
            "slice_cap": slice_cap,
            "keep_recent_turns": None,
            "backlog_messages": 0,
            "backlog_turns": 0,
            "slices": 0,
            "slice_input_tokens": [],
            "max_slice_tokens": None,
            "max_over_slice_cap": False,
            "covered_before": 0,
            "covered_after": 0,
            "cutoff": 0,
            "covered_equals_cutoff": True,
            "summary_chars": 0,
        }
        if plan is None:                                  # 没有可压的旧轮次
            return result

        turns, state, covered, cutoff = plan
        stub = StubLLM()
        summary_after = chatlog.maybe_summarize(npc_id, stub, store_dir=temp_store)
        covered_after = chatlog.load_summary(npc_id, store_dir=temp_store)["covered"]
        per_call = [call["input_tokens"] for call in stub.calls]

        result.update({
            "keep_recent_turns": max(0, (len(turns) - cutoff) // chatlog._MESSAGES_PER_TURN),
            "backlog_messages": max(0, cutoff - covered),
            "backlog_turns": max(0, cutoff - covered) // chatlog._MESSAGES_PER_TURN,
            "slices": len(per_call),
            "slice_input_tokens": per_call,
            "max_slice_tokens": max(per_call) if per_call else None,
            "max_over_slice_cap": bool(per_call) and max(per_call) > slice_cap,
            "covered_before": covered,
            "covered_after": covered_after,
            "cutoff": cutoff,
            "covered_equals_cutoff": covered_after == cutoff,
            "summary_chars": len(summary_after or ""),
        })
        return result
    finally:
        shutil.rmtree(str(temp_root), ignore_errors=True)


def _print_report(result: Dict[str, Any]) -> None:
    print("")
    print("=== 折叠干跑（npc=%s） ===" % result["npc_id"])
    print("存档：%s（已复制到临时目录，原目录未改动）" % result["store"])
    print("窗口：%s   单片上限（窗口 × %.1f）：%s"
          % (result["window"], chatlog._FOLD_SLICE_RATIO, result["slice_cap"]))
    if result["slices"] == 0 and result["cutoff"] == 0:
        print("积压：0 轮 —— 没有可压的旧轮次（保留最近 %s 轮），不需要折叠。"
              % chatlog.summary_keep_recent())
        return
    print("积压：%d 轮（%d 条消息；保留最近 %s 轮）"
          % (result["backlog_turns"], result["backlog_messages"],
             result["keep_recent_turns"]))
    print("分片：%d 片，每片输入 token：%s"
          % (result["slices"], result["slice_input_tokens"]))
    if result["max_slice_tokens"] is None:
        print("最大分片输入：-（一片都没发出去，看日志里的 summary_failed）")
    else:
        print("最大分片输入：%d %s %d（%s）"
              % (result["max_slice_tokens"],
                 ">" if result["max_over_slice_cap"] else "≤",
                 result["slice_cap"],
                 "超出上限，需要调小分片或换更长的窗口" if result["max_over_slice_cap"] else "未超上限"))
    print("covered：%d（起始 %d） / cutoff：%d —— %s"
          % (result["covered_after"], result["covered_before"], result["cutoff"],
             "相等" if result["covered_equals_cutoff"] else "不相等（没折完，见分片与日志）"))
    print("摘要长度：%d 字（桩 LLM 的固定回复，内容不作数）" % result["summary_chars"])


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="老存档折叠干跑（不联网、不动原存档）")
    parser.add_argument("--store", required=True, help="运行时数据目录（含 <npc>_chat.jsonl）")
    parser.add_argument("--npc", required=True, help="NPC id（对应 <npc>_chat.jsonl）")
    parser.add_argument("--window", type=int, default=0,
                        help="上下文窗口（token，可选）；不给用现有设置")
    parser.add_argument("--out", default="", help="结果另存为 JSON（可选）")
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):            # Windows 控制台默认编码不一定 UTF-8
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except Exception:
                pass

    try:
        result = run(Path(args.store), args.npc, args.window or None)
    except (FileNotFoundError, ValueError) as exc:
        print("干跑中止：%s" % exc)
        return 2

    _print_report(result)
    if args.out:
        Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        print("已写 %s" % args.out)
    return 0 if result["covered_equals_cutoff"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
