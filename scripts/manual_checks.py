"""手测脚本 —— 三个子命令（结果人工看，不做自动判分）。

  llm-down    不配 LLM，跑 N 次 /api/talk，统计首帧延迟（中位数 / 最大值）。
  disconnect  发起 /api/talk 后中途断开 SSE，检查服务端：同 NPC 的 asyncio.Lock 是否释放、
              下一次请求是否正常、pending / call_id 是否干净、日志里有无异常。
  uncertain   mock Mod 的 /execute 故意慢于 NPC_EXECUTE_TIMEOUT，触发「不确定」；
              **需要真实 key**，跑 N 轮，把模型每轮台词原样存到 out/，措辞留人工看。

用法：
  python scripts/manual_checks.py llm-down --runs 10
  python scripts/manual_checks.py disconnect
  python scripts/manual_checks.py uncertain --runs 3

说明：
  · 前两个子命令用**进程内** uvicorn（注入临时 roles/store），不碰真实 store/，也不需要真实 key；
  · uncertain 用真实 LLM（环境变量或 api_key.txt 里的 key），仍然用临时 store；
  · 所有耗时都是本机实测，数字只代表这台机器。
"""
from __future__ import annotations

import argparse
import asyncio
import http.client
import json
import logging
import os
import re
import shutil
import socket
import statistics
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.client import LLMClient, LLMResponse       # noqa: E402
from core.paths import Paths                        # noqa: E402
from core.types import StreamChunk                  # noqa: E402

import chatlog                                      # noqa: E402
import server                                       # noqa: E402


# ── 终端输出 ─────────────────────────────────────────────

def _utf8_stdout() -> None:
    """Windows 控制台默认编码不一定是 UTF-8；能改就改。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except Exception:
                pass


def _section(title: str) -> None:
    print("")
    print("=== %s ===" % title)


# ── 进程内服务 ───────────────────────────────────────────

def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


class ServedApp:
    """在后台线程里跑 uvicorn（同进程，方便直接读 app.state）。"""

    def __init__(self, app: Any):
        import uvicorn

        self.app = app
        self.port = _free_port()
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port,
                                log_level="warning", access_log=False)
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def base_url(self) -> str:
        return "http://127.0.0.1:%d" % self.port

    def __enter__(self) -> "ServedApp":
        self.thread.start()
        deadline = time.time() + 15
        while time.time() < deadline:
            if getattr(self.server, "started", False):
                return self
            if not self.thread.is_alive():
                break
            time.sleep(0.05)
        raise RuntimeError("uvicorn 没能在 15s 内启动")

    def __exit__(self, *exc: Any) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


def _temp_paths() -> Paths:
    """临时运行目录：复制一份角色卡，store 从空开始（不污染真实数据）。"""
    root = Path(tempfile.mkdtemp(prefix="npc_manual_"))
    shutil.copytree(str(ROOT / "personas"), str(root / "personas"))
    (root / "store").mkdir(parents=True, exist_ok=True)
    return Paths(root=root)


# ── HTTP 小工具（标准库，不引额外依赖）─────────────────────

def _post_json(url: str, payload: Dict[str, Any], timeout: float = 10.0) -> Tuple[int, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(url, data=body,
                      headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8", "replace"))
    except Exception as exc:
        return 0, {"error": "%s: %s" % (type(exc).__name__, exc)}


def stream_talk(url: str, payload: Dict[str, Any], stop_after: Optional[int] = None,
                timeout: float = 30.0) -> Dict[str, Any]:
    """POST /api/talk 并逐行读 SSE。

    stop_after=N：读到 N 个 data: 帧就**直接断开**（返回 cut=True），用于 disconnect。
    stop_after=None：读到 EOF / done。
    """
    parts = urlsplit(url)
    conn = http.client.HTTPConnection(parts.hostname or "127.0.0.1",
                                      parts.port or 80, timeout=timeout)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "Connection": "close"}
    frames: List[Dict[str, Any]] = []
    error = ""
    cut = False
    t0 = time.perf_counter()
    headers_ms = None
    try:
        conn.request("POST", parts.path or "/", body=body, headers=headers)
        response = conn.getresponse()
        headers_ms = round((time.perf_counter() - t0) * 1000.0, 1)
        while True:
            raw = response.readline()
            if not raw:
                break
            now_ms = round((time.perf_counter() - t0) * 1000.0, 1)
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not line.startswith("data:"):
                continue
            try:
                frame = json.loads(line[len("data:"):].strip())
            except ValueError:
                continue
            frames.append({"at_ms": now_ms, "frame": frame})
            if stop_after is not None and len(frames) >= stop_after:
                cut = True
                break
    except socket.timeout:
        error = "socket timeout"
    except Exception as exc:
        error = "%s: %s" % (type(exc).__name__, exc)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    end_ms = round((time.perf_counter() - t0) * 1000.0, 1)
    return {
        "headers_ms": headers_ms,
        "first_frame_ms": frames[0]["at_ms"] if frames else None,
        "end_ms": end_ms,
        "frames": frames,
        "cut": cut,
        "error": error,
        "text": "".join(f["frame"].get("text", "") for f in frames
                        if f["frame"].get("type") == "delta"),
    }


# ── 假 Provider ──────────────────────────────────────────

class _NullProvider:
    """永远不可用 —— 用来构造「没配 LLM」的 app，再手动把 holder 置空。"""

    model_name = "none"

    def chat(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("no llm")

    async def stream(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("no llm")
        yield  # pragma: no cover


class _SlowActionProvider:
    """第一轮调 cook 动作，之后慢速逐字吐台词（便于中途断连）。"""

    model_name = "manual-check"

    def __init__(self, chunks: int = 30, gap_s: float = 0.05):
        self.chunks = chunks
        self.gap_s = gap_s
        self.stream_calls = 0

    def chat(self, *args: Any, **kwargs: Any) -> LLMResponse:
        raise RuntimeError("本脚本不测非流式调用")

    async def stream(self, *args: Any, **kwargs: Any) -> Any:
        self.stream_calls += 1
        if self.stream_calls == 1:
            yield StreamChunk(tool_call_delta={
                "index": 0, "id": "call_1", "function_name": "cook",
                "function_arguments": json.dumps({"dish": "面"}, ensure_ascii=False),
            }, model=self.model_name, index=1)
            yield StreamChunk(finish_reason="tool_calls", model=self.model_name, index=2)
            return
        for i in range(self.chunks):
            await asyncio.sleep(self.gap_s)
            yield StreamChunk(content="第%d句。" % (i + 1), model=self.model_name, index=i + 3)
        yield StreamChunk(finish_reason="stop", model=self.model_name, index=self.chunks + 3)


# ── mock Mod（/execute）───────────────────────────────────

class _MockModHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    delay = 0.0
    calls: List[Dict[str, Any]] = []

    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            payload = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            payload = {}
        _MockModHandler.calls.append(payload)
        if self.delay > 0:
            time.sleep(self.delay)
        body = json.dumps({"ok": True, "status": "done", "note": "mock mod 执行完成"},
                          ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


def start_mock_mod(delay: float = 0.0) -> Tuple[Any, str]:
    _MockModHandler.delay = delay
    _MockModHandler.calls = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _MockModHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, "http://127.0.0.1:%d/execute" % httpd.server_address[1]


@contextmanager
def env_var(name: str, value: Optional[str]) -> Iterator[None]:
    old = os.environ.get(name)
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value
    try:
        yield
    finally:
        if old is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = old


# ── 日志捕获 ─────────────────────────────────────────────

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _clean(text: str) -> str:
    return _ANSI_RE.sub("", text or "").strip()


class LogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.records: List[Dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = _clean(record.getMessage())
        except Exception:
            message = _clean(str(record.msg))
        self.records.append({
            "level": record.levelname,
            "logger": record.name,
            "message": message,
            "exc_info": bool(record.exc_info),
        })

    def suspicious(self) -> List[Dict[str, Any]]:
        """真正的异常迹象（区别于业务上预期的 warning）。"""
        markers = ("Task exception", "never retrieved", "GeneratorExit",
                   "CancelledError", "Traceback", "Exception in ASGI")
        return [r for r in self.records
                if r["exc_info"] or any(m in r["message"] for m in markers)]


# ── 子命令 1：llm-down ───────────────────────────────────

def cmd_llm_down(args: argparse.Namespace) -> int:
    paths = _temp_paths()
    provider = _NullProvider()
    app = server.create_app(llm_client=LLMClient(provider=provider), paths=paths)
    app.state.llm_holder["client"] = None          # 强制大脑不可用 = 「没配 LLM」

    with ServedApp(app) as served:
        url = served.base_url + "/api/talk"
        print("URL: %s" % url)
        print("轮数: %d（LLM 强制不可用，走角色卡 rules 兜底）" % args.runs)
        rows = []
        for i in range(1, args.runs + 1):
            result = stream_talk(url, {"npc_id": "cang", "message": "在吗"},
                                 timeout=args.timeout)
            types = [f["frame"].get("type") for f in result["frames"]]
            rows.append(result)
            print("  第 %2d 次: 首帧 %8.1f ms | 总 %8.1f ms | 帧 %s | %s"
                  % (i,
                     result["first_frame_ms"] if result["first_frame_ms"] is not None else -1,
                     result["end_ms"], ",".join(str(t) for t in types),
                     ("错误: " + result["error"]) if result["error"] else result["text"][:20]))
            if result["frames"] and result["frames"][-1]["frame"].get("type") != "done":
                print("     ! 最后一帧不是 done")

    firsts = [r["first_frame_ms"] for r in rows if r["first_frame_ms"] is not None]
    if not firsts:
        print("没有拿到任何首帧，无法统计")
        return 1
    med = statistics.median(firsts)
    maximum = max(firsts)
    _section("llm-down 汇总")
    print("首帧延迟（ms）：中位数 %.1f，最大 %.1f，最小 %.1f，样本 %d"
          % (med, maximum, min(firsts), len(firsts)))
    print(json.dumps({"check": "llm-down", "runs": len(firsts),
                      "first_frame_ms": {"median": round(med, 1), "max": round(maximum, 1),
                                         "min": round(min(firsts), 1)},
                      "rows": [{"first_frame_ms": r["first_frame_ms"],
                                "total_ms": r["end_ms"],
                                "frames": [f["frame"].get("type") for f in r["frames"]],
                                "error": r["error"] or None} for r in rows]},
                     ensure_ascii=False))
    shutil.rmtree(str(paths.root), ignore_errors=True)
    return 0 if all(not r["error"] for r in rows) else 1


# ── 子命令 2：disconnect ─────────────────────────────────

def cmd_disconnect(args: argparse.Namespace) -> int:
    capture = LogCapture()
    root_logger = logging.getLogger()
    root_logger.addHandler(capture)
    root_logger.setLevel(logging.WARNING)      # 只看 warning 以上，避免 httpx 的 INFO 噪音
    for noisy in ("httpx", "httpcore", "uvicorn"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    paths = _temp_paths()
    httpd, execute_url = start_mock_mod(delay=args.mod_delay)
    provider = _SlowActionProvider()

    app = server.create_app(llm_client=LLMClient(provider=provider), paths=paths)
    turn_env = env_var("NPC_EXECUTE_TIMEOUT", str(args.execute_timeout))
    with turn_env:
        with ServedApp(app) as served:
            base = served.base_url
            _post_json(base + "/api/capabilities", {
                "mod": "manual", "execute_url": execute_url,
                "actions": [{"name": "cook", "desc": "做一道菜",
                             "params": {"dish": "菜名"}}]})

            before_turns = chatlog.load_turns("cang", store_dir=paths.store_dir)
            first = stream_talk(base + "/api/talk",
                                {"npc_id": "cang", "message": "帮我做碗面", "mod": "manual"},
                                stop_after=1, timeout=args.timeout)
            print("中途断开：读到 %d 帧后关闭连接（首帧 %s ms，cut=%s）"
                  % (len(first["frames"]), first["first_frame_ms"], first["cut"]))
            time.sleep(0.8)

            pending_after_cut = [dict(p) for p in app.state.pending.alive("cang")]

            second = stream_talk(base + "/api/talk",
                                 {"npc_id": "cang", "message": "在吗", "mod": "manual"},
                                 timeout=args.timeout)
            pending_after_second = [dict(p) for p in app.state.pending.alive("cang")]
            after_turns = chatlog.load_turns("cang", store_dir=paths.store_dir)
    httpd.shutdown()

    types = [f["frame"].get("type") for f in second["frames"]]
    got_done = bool(types) and types[-1] == "done"
    lock_released = got_done and second["end_ms"] < args.timeout * 1000
    clean_calls = [p for p in pending_after_second
                   if isinstance(p.get("call_id"), str) and len(p["call_id"]) == 32]
    problems = capture.suspicious()

    _section("disconnect 检查结果")
    print("1) Lock 释放 / 下一次请求：%s（第二请求总耗时 %.1f ms，帧 %s，错误 %s）"
          % ("正常" if lock_released else "异常", second["end_ms"],
             ",".join(str(t) for t in types), second["error"] or "无"))
    print("2) pending（断开后）：%d 条" % len(pending_after_cut))
    for p in pending_after_cut:
        print("   action=%s state=%s call_id=%s（%s 秒前）"
              % (p.get("action"), p.get("state"), p.get("call_id"),
                 round(time.time() - float(p.get("created_at", time.time())), 1)))
    print("3) pending（第二次请求后）：%d 条；call_id 格式正常 %d 条"
          % (len(pending_after_second), len(clean_calls)))
    print("4) 聊天记录：断连前 %d 行 → 断连+二次请求后 %d 行（断连回合是否落盘）"
          % (len(before_turns), len(after_turns)))
    print("5) 日志异常迹象：%d 条" % len(problems))
    for item in problems[:5]:
        print("   [%s] %s" % (item["level"], item["message"][:200]))
    print("   本次 WARNING/ERROR 共 %d 条（含预期内的 action_execute_timeout）"
          % len(capture.records))
    for item in capture.records:
        print("   - [%s] %s" % (item["level"], item["message"][:200]))

    verdict = {
        "check": "disconnect",
        "lock_released": lock_released,
        "next_request_ok": got_done and not second["error"],
        "pending_after_cut": [{"action": p.get("action"), "state": p.get("state"),
                               "call_id": p.get("call_id")} for p in pending_after_cut],
        "pending_after_second": [{"action": p.get("action"), "state": p.get("state"),
                                  "call_id": p.get("call_id")} for p in pending_after_second],
        "call_id_well_formed": len(clean_calls) == len(pending_after_second),
        "chat_lines_before": len(before_turns),
        "chat_lines_after": len(after_turns),
        "interrupted_turn_persisted": len(after_turns) > len(before_turns),
        "suspicious_logs": problems,
        "warnings": [r["message"] for r in capture.records],
    }
    print("结果 JSON: " + json.dumps(verdict, ensure_ascii=False))

    root_logger.removeHandler(capture)
    shutil.rmtree(str(paths.root), ignore_errors=True)
    return 0 if (lock_released and not problems) else 1


# ── 子命令 3：uncertain ──────────────────────────────────

def cmd_uncertain(args: argparse.Namespace) -> int:
    from core import config

    key = config.api_key()
    if not key:
        print("未检测到真实 key：请配置 NPC_API_KEY（或 api_key.txt），再跑 uncertain。")
        return 2

    paths = _temp_paths()
    httpd, execute_url = start_mock_mod(delay=args.delay)
    app = server.create_app(llm_client=None, paths=paths)
    if app.state.llm_holder["client"] is None:
        print("LLM 三件套未配齐（模型名 / key / 端点），uncertain 需要真实模型。")
        httpd.shutdown()
        shutil.rmtree(str(paths.root), ignore_errors=True)
        return 2

    out_dir = Path(args.out) / ("uncertain_%s" % time.strftime("%Y%m%d_%H%M%S"))
    out_dir.mkdir(parents=True, exist_ok=True)
    rounds: List[Dict[str, Any]] = []

    with env_var("NPC_EXECUTE_TIMEOUT", str(args.execute_timeout)):
        with ServedApp(app) as served:
            base = served.base_url
            _post_json(base + "/api/capabilities", {
                "mod": "manual", "execute_url": execute_url,
                "actions": [{"name": "cook", "desc": "做一道菜",
                             "params": {"dish": "菜名"}}]})
            for i in range(1, args.runs + 1):
                result = stream_talk(base + "/api/talk",
                                     {"npc_id": "cang", "message": args.message,
                                      "mod": "manual"},
                                     timeout=args.timeout)
                actions = [f["frame"].get("action") for f in result["frames"]
                           if f["frame"].get("type") == "action"]
                rounds.append({"round": i, "text": result["text"],
                               "actions": actions, "error": result["error"]})
                path = out_dir / ("round_%d.txt" % i)
                path.write_text(
                    "轮次: %d\n输入: %s\n动作帧: %s\n错误: %s\n\n台词原文:\n%s\n"
                    % (i, args.message, json.dumps(actions, ensure_ascii=False),
                       result["error"] or "无", result["text"]),
                    encoding="utf-8")
                print("第 %d 轮已存 %s（%d 字，动作帧 %d 个%s）"
                      % (i, path, len(result["text"]), len(actions),
                         "，含 unknown" if any((a or {}).get("status") == "unknown"
                                               for a in actions) else ""))
    httpd.shutdown()
    (out_dir / "meta.json").write_text(
        json.dumps({"message": args.message, "rounds": rounds},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print("输出目录: %s" % out_dir)
    print("请人工看措辞；结果栏由人填。")
    shutil.rmtree(str(paths.root), ignore_errors=True)
    return 0


# ── 入口 ─────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="NPCSidekick v4 手测脚本")
    sub = parser.add_subparsers(dest="command", required=True)

    p1 = sub.add_parser("llm-down", help="不配 LLM，测 /api/talk 首帧延迟")
    p1.add_argument("--runs", type=int, default=10)
    p1.add_argument("--timeout", type=float, default=20.0)
    p1.set_defaults(func=cmd_llm_down)

    p2 = sub.add_parser("disconnect", help="中途断开 SSE，检查锁 / pending / 日志")
    p2.add_argument("--timeout", type=float, default=10.0)
    p2.add_argument("--execute-timeout", type=float, default=0.3,
                    help="NPC_EXECUTE_TIMEOUT（秒），要小于 --mod-delay 才能触发 unknown")
    p2.add_argument("--mod-delay", type=float, default=2.0,
                    help="mock Mod 的 /execute 延迟（秒）")
    p2.set_defaults(func=cmd_disconnect)

    p3 = sub.add_parser("uncertain", help="mock Mod 超时触发「不确定」（需要真实 key）")
    p3.add_argument("--runs", type=int, default=3)
    p3.add_argument("--timeout", type=float, default=180.0)
    p3.add_argument("--delay", type=float, default=2.0,
                    help="mock Mod 的 /execute 延迟（秒）")
    p3.add_argument("--execute-timeout", type=float, default=0.3)
    p3.add_argument("--message", default="我有点饿了，你能给我做碗面吗？")
    p3.add_argument("--out", default=str(ROOT / "out"))
    p3.set_defaults(func=cmd_uncertain)
    return parser


def main() -> int:
    _utf8_stdout()
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
