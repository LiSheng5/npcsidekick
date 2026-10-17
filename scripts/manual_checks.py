#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""手测脚本化 —— 三个子命令，各自起一个真实的本地服务（同进程后台线程，可看 app.state）。

  llm-down    不配 LLM，测 /api/talk 首帧延迟，跑 N 次，输出中位数和最大值
  disconnect  发起 /api/talk 后中途断开 SSE，检查锁是否释放 / 下一次请求是否正常 /
              pending 与 call_id 是否干净 / 日志里有没有异常
  uncertain   mock Mod 的 /execute 故意超时 → 触发「不确定」；需要真实 key，
              把模型每轮台词原样存到 out/ 目录供人工看措辞（不做自动判分）

用法：
  python scripts/manual_checks.py llm-down --runs 5
  python scripts/manual_checks.py disconnect
  python scripts/manual_checks.py uncertain --runs 3 --delay 40

说明：
  · 前两项零网络（假 Provider / 临时目录），不会动工程里真实的 store/ 与角色卡
  · llm-down 会临时清掉进程内的 key 环境变量，确保「不配 LLM」
  · 每项结束都打一行 JSON 汇总，方便直接贴回来
"""
import argparse
import asyncio
import contextlib
import gc
import json
import logging
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import http.client                          # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402

import uvicorn                              # noqa: E402

from core import logging_config as _logging_config  # noqa: E402

# import server 时会顺手建一个默认 app（会打一条 llm_not_configured）——
# 先把日志压到 ERROR，报告里就不会混进这条启动噪音。
_logging_config.configure_logging(level="ERROR", mode="test")
_logging_config._LazyLogger._configured = True      # 别让懒初始化把级别改回 INFO

import chatlog                              # noqa: E402
import memory                               # noqa: E402
import server                               # noqa: E402
from core.client import LLMClient           # noqa: E402
from core.paths import Paths, default_paths  # noqa: E402
from core.types import StreamChunk          # noqa: E402

HOST = "127.0.0.1"
KEY_ENVS = ("NPC_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ZHIPU_API_KEY")


# ── 通用：起服务 / 发请求 ────────────────────────────────

def free_port():
    sock = socket.socket()
    try:
        sock.bind((HOST, 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


class LiveServer(object):
    """在后台线程里跑一个真实 uvicorn（同进程 → 可以直接看 app.state）。"""

    def __init__(self, app):
        self.app = app
        self.port = free_port()
        self._server = uvicorn.Server(uvicorn.Config(
            app, host=HOST, port=self.port, log_level="warning",
            access_log=False, lifespan="off", log_config=None))
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def __enter__(self):
        self._thread.start()
        deadline = time.time() + 15
        while time.time() < deadline:
            if self._server.started:
                self._wait_ready()
                return self
            time.sleep(0.02)
        raise RuntimeError("uvicorn 未能在 15s 内启动")

    def _wait_ready(self):
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                status, _ = request_json(self.port, "GET", "/api/state")
                if status == 200:
                    return
            except Exception:
                pass
            time.sleep(0.05)
        raise RuntimeError("/api/state 一直没就绪")

    def __exit__(self, *exc_info):
        self._server.should_exit = True
        self._thread.join(timeout=8)


def request_json(port, method, path, payload=None, timeout=30.0):
    """普通 JSON 请求 → (status, 解析后的 body 或原始文本)。"""
    conn = http.client.HTTPConnection(HOST, port, timeout=timeout)
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"} if body is not None else {}
    try:
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", "replace")
        try:
            return resp.status, json.loads(raw)
        except ValueError:
            return resp.status, raw
    finally:
        conn.close()


def talk_stream(port, npc_id, message, timeout=60.0, max_frames=None, mod=None):
    """POST /api/talk 逐行读 SSE。

    max_frames 给了 = 读够这么多帧就主动关连接（模拟 Mod 中途掉线）。
    返回 {status, first_frame_ms, total_ms, frames, aborted}。
    """
    started = time.perf_counter()
    conn = http.client.HTTPConnection(HOST, port, timeout=timeout)
    payload = {"npc_id": npc_id, "message": message}
    if mod:
        payload["mod"] = mod
    body = json.dumps(payload).encode("utf-8")
    frames = []
    first_ms = None
    aborted = False
    try:
        conn.request("POST", "/api/talk", body=body,
                     headers={"Content-Type": "application/json",
                              "Accept": "text/event-stream"})
        resp = conn.getresponse()
        status = resp.status
        while True:
            line = resp.readline()
            if not line:
                break
            if not line.startswith(b"data: "):
                continue
            now = time.perf_counter()
            if first_ms is None:
                first_ms = (now - started) * 1000.0
            frames.append(json.loads(line[len(b"data: "):].decode("utf-8")))
            if max_frames is not None and len(frames) >= max_frames:
                aborted = True
                break
    finally:
        conn.close()
    return {"status": status, "first_frame_ms": first_ms,
            "total_ms": (time.perf_counter() - started) * 1000.0,
            "frames": frames, "aborted": aborted}


def frames_text(frames):
    return "".join(f.get("text", "") for f in frames if f.get("type") == "delta")


def action_frames(frames):
    return [f.get("action") for f in frames if f.get("type") == "action"]


# ── 通用：临时运行目录 / 环境变量 ─────────────────────────

def temp_root():
    """临时运行时目录：复制真实角色卡（保持提示词体量），store 是空的。"""
    root = Path(tempfile.mkdtemp(prefix="npc_manual_"))
    src = ROOT / "personas"
    if src.is_dir():
        shutil.copytree(str(src), str(root / "personas"))
    (root / "store").mkdir(parents=True, exist_ok=True)
    return root


@contextlib.contextmanager
def env_patch(**values):
    """临时改环境变量（None = 删除），退出时恢复。"""
    saved = {}
    for name, value in values.items():
        saved[name] = os.environ.get(name)
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = str(value)
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def median(values):
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


class LogSink(object):
    """接住根 logger 的日志：收集到 records，同时把控制台输出摘掉（报告才干净）。"""

    def __init__(self):
        self.records = []
        self._root = logging.getLogger()
        self._saved_handlers = []
        self._saved_level = None

        sink = self

        class Handler(logging.Handler):
            def emit(self, record):
                message = _ANSI_RE.sub("", record.getMessage())
                sink.records.append({
                    "level": record.levelname,
                    "logger": record.name,
                    "message": message,
                })

        self._handler = Handler(level=logging.INFO)

    def __enter__(self):
        self._saved_handlers = self._root.handlers[:]
        self._saved_level = self._root.level
        for handler in self._saved_handlers:
            self._root.removeHandler(handler)
        # 留着我们这一个 handler，logging.basicConfig 就不会再加控制台 handler
        self._root.addHandler(self._handler)
        self._root.setLevel(logging.INFO)
        return self

    def __exit__(self, *exc_info):
        self._root.removeHandler(self._handler)
        for handler in self._saved_handlers:
            self._root.addHandler(handler)
        self._root.setLevel(self._saved_level)


# ── 子命令 1：llm-down ───────────────────────────────────

def cmd_llm_down(args):
    """不配 LLM：测 /api/talk 首帧延迟（角色卡 rules 兜底回复那条路径）。"""
    root = temp_root()
    rows = []
    try:
        with env_patch(NPC_API_KEY=None, DEEPSEEK_API_KEY=None,
                       OPENAI_API_KEY=None, ZHIPU_API_KEY=None), LogSink():
            app = server.create_app(llm_client=None, paths=Paths(root=root))
            if app.state.llm_holder["client"] is not None:
                print("注意：环境里有可用的 LLM 配置，已强制置空（本项要求「不配 LLM」）")
                app.state.llm_holder["client"] = None
            with LiveServer(app) as srv:
                for index in range(1, args.runs + 1):
                    got = talk_stream(srv.port, args.npc, args.message,
                                      timeout=args.timeout)
                    rows.append({
                        "run": index,
                        "first_frame_ms": None if got["first_frame_ms"] is None
                        else round(got["first_frame_ms"], 1),
                        "total_ms": round(got["total_ms"], 1),
                        "frames": [f.get("type") for f in got["frames"]],
                        "reply": frames_text(got["frames"]),
                    })
                    print("第 %d 次: 首帧 %s ms / 总 %s ms / 帧 %s"
                          % (index, rows[-1]["first_frame_ms"],
                             rows[-1]["total_ms"], rows[-1]["frames"]))
    finally:
        shutil.rmtree(str(root), ignore_errors=True)

    values = [r["first_frame_ms"] for r in rows if r["first_frame_ms"] is not None]
    summary = {
        "check": "llm-down",
        "npc": args.npc,
        "message": args.message,
        "runs": args.runs,
        "first_frame_ms": values,
        "median_ms": None if not values else round(median(values), 1),
        "max_ms": None if not values else round(max(values), 1),
        "replies": [r["reply"] for r in rows],
    }
    print("")
    print("中位数: %s ms   最大值: %s ms" % (summary["median_ms"], summary["max_ms"]))
    print(json.dumps(summary, ensure_ascii=False))
    return 0


# ── 假 Provider（disconnect 用）：按脚本吐字 + 工具调用 ──────

class ScriptedProvider(object):
    """零网络假 Provider：每次 stream() 取一个脚本。

    脚本 = {"pieces": [...], "piece_delay": 0.15, "tool_call": {"name":…, "arguments":…}}
    """

    def __init__(self, scripts):
        self._scripts = list(scripts)
        self._used = 0

    @property
    def model_name(self):
        return "manual-check-scripted"

    def chat(self, *args, **kwargs):
        raise RuntimeError("手测假 Provider 只实现流式（chat 不该被调用）")

    async def stream(self, messages, tools=None, tool_choice="auto", temperature=None,
                     max_tokens=None, response_format=None, reasoning_effort=None):
        if self._used < len(self._scripts):
            script = self._scripts[self._used]
        else:
            script = {"pieces": ["嗯。"]}
        self._used += 1
        delay = script.get("piece_delay", 0.15)
        index = 0
        for piece in script.get("pieces", []):
            if delay:
                await asyncio.sleep(delay)
            index += 1
            yield StreamChunk(content=piece, model=self.model_name, index=index)
        call = script.get("tool_call")
        if call:
            index += 1
            yield StreamChunk(
                tool_call_delta={"index": 0, "id": "manual_call_1",
                                 "function_name": call.get("name", ""),
                                 "function_arguments": None},
                model=self.model_name, index=index)
            index += 1
            yield StreamChunk(
                tool_call_delta={"index": 0, "id": None, "function_name": None,
                                 "function_arguments": json.dumps(
                                     call.get("arguments") or {}, ensure_ascii=False)},
                model=self.model_name, index=index)
        index += 1
        yield StreamChunk(finish_reason="tool_calls" if call else "stop",
                          model=self.model_name, index=index)


def make_timeout_executor(payloads):
    """假执行器：记下请求体，然后一直不返回（等 wait_for 超时 → 不确定）。"""
    async def executor(url, payload, timeout):
        payloads.append(payload)
        await asyncio.sleep(5.0)
        return {"ok": True, "status": "done", "note": "不该走到这里"}
    return executor


# ── 子命令 2：disconnect ─────────────────────────────────

def cmd_disconnect(args):
    """中途断开 SSE，检查锁 / 二次请求 / pending 与 call_id / 日志。"""
    root = temp_root()
    npc_id = args.npc
    payloads = []
    passed = {}
    detail = {}
    sink = LogSink()
    records = sink.records
    try:
        provider = ScriptedProvider([
            {   # 第 1 轮：先吐字，再调动作（执行器会超时 → 不确定）
                "pieces": ["我", "去", "看", "看", "门", "口"],
                "piece_delay": 0.15,
                "tool_call": {"name": "close_door", "arguments": {"door": "正门"}},
            },
            {   # 第 2 轮：继续吐字（客户端在这里掉线）
                "pieces": ["门", "已", "经", "关", "上", "了", "嘛", "别", "催", "我"],
                "piece_delay": 0.2,
            },
            {   # 第 2 次请求：正常收尾
                "pieces": ["门", "关", "好", "了。"],
                "piece_delay": 0.01,
            },
        ])
        # 执行等待 0.2s 必超时；pending TTL 先给 5s（够我们看快照），之后改成 0 验证清理
        with env_patch(NPC_EXECUTE_TIMEOUT="0.2", NPC_PENDING_TTL="5"), sink:
            app = server.create_app(llm_client=LLMClient(provider=provider),
                                    executor=make_timeout_executor(payloads),
                                    paths=Paths(root=root))
            with LiveServer(app) as srv:
                status, _ = request_json(srv.port, "POST", "/api/capabilities", {
                    "mod": "manual_mod",
                    "execute_url": "http://127.0.0.1:1/execute",
                    "actions": [{"name": "close_door", "desc": "把门关上",
                                 "params": {"door": "哪扇门"}}],
                })
                if status != 200:
                    raise RuntimeError("mod 报到失败: %s" % status)

                # ① 发起 /api/talk，读 8 帧后直接关连接
                first = talk_stream(srv.port, npc_id, "去把门关上", timeout=30,
                                    max_frames=args.abort_after, mod="manual_mod")
                time.sleep(0.8)                 # 等服务端把取消处理完

                pending_after = app.state.pending.alive(npc_id)
                locked = locked_locks()
                entries_after = memory.load_card(npc_id, store_dir=root / "store")
                action_entries = [e for e in entries_after
                                  if e.get("category") == "action"]
                turns = chatlog.load_turns(npc_id, store_dir=root / "store")

                payload = payloads[-1] if payloads else {}
                detail["abort_after_frames"] = len(first["frames"])
                detail["pending_after_disconnect"] = pending_after
                detail["locked_asyncio_locks"] = locked
                detail["memory_action_entries"] = len(action_entries)
                detail["chat_messages"] = len(turns)
                passed["stream_started"] = first["first_frame_ms"] is not None
                passed["pending_one_unknown"] = (
                    len(pending_after) == 1
                    and pending_after[0].get("state") == "unknown"
                    and pending_after[0].get("action") == "close_door"
                    and pending_after[0].get("npc_id") == npc_id)
                passed["call_id_matches_mod_payload"] = (
                    bool(pending_after) and bool(payload)
                    and pending_after[0].get("call_id") == payload.get("call_id"))
                passed["no_card_written_for_unknown"] = len(action_entries) == 0
                passed["lock_released_direct"] = locked == 0
                passed["chat_turn_persisted"] = len(turns) == 2

                # ② TTL 归零后再发同一 NPC：既验证锁已释放，也验证过期待定被清理
                with env_patch(NPC_PENDING_TTL="0"):
                    second = talk_stream(srv.port, npc_id, "门关好了吗", timeout=30,
                                         mod="manual_mod")
                entries_later = memory.load_card(npc_id, store_dir=root / "store")
                expired = [e for e in entries_later
                           if e.get("category") == "action"
                           and e.get("content", "").startswith(memory.EV_FAIL)]
                detail["second_request"] = {
                    "first_frame_ms": None if second["first_frame_ms"] is None
                    else round(second["first_frame_ms"], 1),
                    "total_ms": round(second["total_ms"], 1),
                    "frame_types": [f.get("type") for f in second["frames"]],
                    "reply": frames_text(second["frames"]),
                }
                detail["pending_after_ttl"] = len(app.state.pending)
                detail["expired_failure_entries"] = [e.get("content") for e in expired]
                passed["second_request_ok"] = (
                    second["status"] == 200
                    and second["first_frame_ms"] is not None
                    and any(f.get("type") == "done" for f in second["frames"]))
                passed["lock_released_by_second_request"] = (
                    second["first_frame_ms"] is not None
                    and second["first_frame_ms"] < 3000)
                passed["pending_cleared_after_ttl"] = len(app.state.pending) == 0
                passed["expire_wrote_failure_memory"] = (
                    len(expired) == 1
                    and server.PENDING_EXPIRED_NOTE in expired[0].get("content", ""))
    finally:
        shutil.rmtree(str(root), ignore_errors=True)

    errors = [r for r in records if r["level"] in ("ERROR", "CRITICAL")]
    suspicious = [r for r in records
                  if "Traceback" in r["message"] or "never retrieved" in r["message"]]
    warnings = [r for r in records if r["level"] == "WARNING"]
    passed["no_error_logs"] = not errors and not suspicious

    summary = {
        "check": "disconnect",
        "npc": npc_id,
        "passed": passed,
        "all_passed": all(passed.values()),
        "detail": detail,
        "logs": {
            "error": errors,
            "suspicious": suspicious,
            "warnings": [w["logger"] + ": " + w["message"][:160] for w in warnings],
        },
    }
    print("")
    for name in sorted(passed):
        print("  [%s] %s" % ("通过" if passed[name] else "失败", name))
    print("")
    print("pending 快照: %s" % json.dumps(detail.get("pending_after_disconnect"),
                                       ensure_ascii=False))
    print("二次请求: %s" % json.dumps(detail.get("second_request"), ensure_ascii=False))
    print("警告日志 %d 条 / 错误 %d 条" % (len(warnings), len(errors)))
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["all_passed"] else 1


def locked_locks():
    """当前处于 locked 状态的 asyncio.Lock（直接看锁，不猜）。"""
    found = 0
    for obj in gc.get_objects():
        try:
            if isinstance(obj, asyncio.Lock) and obj.locked():
                found += 1
        except Exception:
            continue
    return found


# ── 子命令 3：uncertain（需要真实 key）────────────────────

class MockMod(object):
    """假 Mod：/execute 先睡 delay 秒再回 —— 让大脑同步等待超时（触发「不确定」）。"""

    def __init__(self, delay):
        self.delay = delay
        self.payloads = []
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except ValueError:
                    payload = {}
                mock.payloads.append(payload)
                time.sleep(mock.delay)
                body = json.dumps({"ok": True, "status": "done",
                                   "note": "门关上了"}).encode("utf-8")
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except Exception:
                    pass                       # 客户端早走了，正常

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer((HOST, 0), Handler)
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def execute_url(self):
        return "http://%s:%d/execute" % (HOST, self.port)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc_info):
        self.httpd.shutdown()
        self.httpd.server_close()


def cmd_uncertain(args):
    """mock Mod 超时 → 不确定；把每轮台词原样存 out/（不做自动判分）。"""
    from core.llm_runtime import llm_config_status

    status = llm_config_status()
    if not status.get("ready"):
        print("LLM 没配齐，缺: %s —— 本项需要真实 key，先配 AGENT_MODEL / NPC_API_KEY / "
              "NPC_BASE_URL（或控制台「模型」页）" % "、".join(status.get("missing") or []))
        return 2

    limit = server.execute_timeout()
    if args.delay <= limit:
        print("注意：--delay %.1fs 不大于 NPC_EXECUTE_TIMEOUT %.1fs，可能不会触发「不确定」"
              % (args.delay, limit))

    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out) / ("uncertain_" + stamp)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    with MockMod(args.delay) as mod:
        print("mock Mod: %s（execute 延迟 %.0fs）" % (mod.execute_url, args.delay))
        app = server.create_app(paths=default_paths())
        with LiveServer(app) as srv:
            for round_no in range(1, args.runs + 1):
                caps_status, _ = request_json(srv.port, "POST", "/api/capabilities", {
                    "mod": "mock_mod",
                    "execute_url": mod.execute_url,
                    "actions": [{"name": args.action, "desc": args.desc,
                                 "params": {"door": "哪扇门"}}],
                })
                if caps_status != 200:
                    raise RuntimeError("mod 报到失败: %s" % caps_status)
                got = talk_stream(srv.port, args.npc, args.message, timeout=args.timeout)
                dialogue = frames_text(got["frames"])
                actions = action_frames(got["frames"])
                row = {
                    "round": round_no,
                    "first_frame_ms": None if got["first_frame_ms"] is None
                    else round(got["first_frame_ms"], 1),
                    "total_ms": round(got["total_ms"], 1),
                    "dialogue": dialogue,
                    "actions": actions,
                }
                rows.append(row)
                path = out_dir / ("round_%02d.txt" % round_no)
                path.write_text(round_record_text(args, row), encoding="utf-8")
                print("第 %d 轮 → %s（动作帧 status: %s）"
                      % (round_no, path.name,
                         [a.get("status") for a in actions] or "无"))

    summary = {
        "check": "uncertain",
        "npc": args.npc,
        "message": args.message,
        "action": args.action,
        "delay_s": args.delay,
        "runs": args.runs,
        "out_dir": str(out_dir),
        "rounds": rows,
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0


def round_record_text(args, row):
    """一轮的原始记录（台词原样，供人工看措辞）。"""
    lines = [
        "轮次: %d" % row["round"],
        "NPC: %s" % args.npc,
        "玩家: %s" % args.message,
        "mock Mod 执行延迟: %.1fs" % args.delay,
        "首帧: %s ms  总耗时: %s ms" % (row["first_frame_ms"], row["total_ms"]),
        "",
        "--- 台词（原样）---",
        row["dialogue"] or "（这一轮没有台词）",
        "",
        "--- 动作帧（已执行动作的记录）---",
        json.dumps(row["actions"], ensure_ascii=False, indent=2),
        "",
    ]
    return "\n".join(lines)


# ── 入口 ─────────────────────────────────────────────────

def build_parser():
    parser = argparse.ArgumentParser(description="NPCSidekick 手测脚本（三个子命令）")
    sub = parser.add_subparsers(dest="command")

    p1 = sub.add_parser("llm-down", help="不配 LLM，测 /api/talk 首帧延迟")
    p1.add_argument("--runs", type=int, default=5, help="跑几次（默认 5）")
    p1.add_argument("--npc", default="cang", help="用哪张角色卡（默认 cang）")
    p1.add_argument("--message", default="在吗", help="玩家说的话")
    p1.add_argument("--timeout", type=float, default=30.0, help="单次请求超时（秒）")
    p1.set_defaults(func=cmd_llm_down)

    p2 = sub.add_parser("disconnect", help="中途断开 SSE，检查服务端状态")
    p2.add_argument("--npc", default="cang", help="用哪张角色卡（默认 cang）")
    p2.add_argument("--abort-after", type=int, default=8,
                    help="读够多少帧就断开（默认 8）")
    p2.set_defaults(func=cmd_disconnect)

    p3 = sub.add_parser("uncertain", help="mock Mod 超时 → 触发「不确定」（需要真实 key）")
    p3.add_argument("--runs", type=int, default=3, help="跑几轮（默认 3）")
    p3.add_argument("--delay", type=float, default=40.0,
                    help="mock Mod 的 /execute 延迟秒数（要大于 NPC_EXECUTE_TIMEOUT）")
    p3.add_argument("--npc", default="cang", help="用哪张角色卡（默认 cang）")
    p3.add_argument("--message", default="帮我把门关上", help="玩家说的话")
    p3.add_argument("--action", default="close_door", help="mock Mod 声明的动作名")
    p3.add_argument("--desc", default="把门关上", help="动作说明（给模型看）")
    p3.add_argument("--out", default=str(ROOT / "out"), help="台词落盘目录（默认 out/）")
    p3.add_argument("--timeout", type=float, default=180.0, help="单轮请求超时（秒）")
    p3.set_defaults(func=cmd_uncertain)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    sys.exit(main())
