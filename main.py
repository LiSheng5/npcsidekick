#!/usr/bin/env python3
"""
奇天 v1.0 — 主入口点

运行:
  python main.py                  # 交互模式
  python main.py "你的问题"       # 单次查询
  python main.py --stream         # 流式交互模式 (Rich Live)
  python main.py --stream "问题"  # 流式单次查询
  python main.py --model gpt-4    # 指定模型

命令 (交互模式):
  exit / quit         — 退出
  help / 帮助         — 显示帮助
  plan                — 显示当前任务计划
  memory              — 显示记忆摘要
  tools               — 列出所有可用工具
  history             — 显示最近的执行记录
  stream              — 切换到流式模式
"""
import argparse
import asyncio
import io
import json
import os
import sys
import time

# Fix Windows GBK encoding for emoji
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from rich.console import Console
from rich.prompt import Prompt
from rich.panel import Panel
from rich.text import Text
from rich.live import Live
from rich.align import Align

from agent.orchestrator import AgentOrchestrator
from agent.display import AgentDisplay
import config

console = Console()


# ══════════════════════════════════════════════════════════════
# 紫色太阳 Logo 启动动画
# ══════════════════════════════════════════════════════════════

# 4 帧光芒旋转 — 外围 "刺" 绕紫色圆球旋转
_SUN_FRAMES = [
    # 十字方向
    """   ╲   ╱
     ╲ │ ╱
    ───◉───
     ╱ │ ╲
      ╱   ╲""",
    # 对角方向 (右上→左下)
    """    │
      ╲   ╱
     ╲  ◉  ╱
      ╱   ╲
        │""",
    # 对角方向 (左上→右下)
    """    │
      ╱   ╲
     ╱  ◉  ╲
      ╲   ╱
        │""",
    # 十字方向 (交替)
    """   ╱   ╲
     ╱ │ ╲
    ───◉───
     ╲ │ ╱
      ╲   ╱""",
]


def play_intro_animation():
    """播放紫色太阳 Logo 动画 — 1.5 秒，外围光芒旋转。"""
    frames = []
    for art in _SUN_FRAMES:
        frames.append(Align.center(Text(art, style="bold #9b59b6")))

    try:
        with Live(frames[0], console=console, refresh_per_second=12, transient=True) as live:
            # 旋转约 1.5 秒 (4 帧 × 3 圈)
            for _ in range(3):
                for frame in frames:
                    time.sleep(0.12)
                    live.update(frame)
    except Exception:
        pass  # 动画失败不影响功能


# ══════════════════════════════════════════════════════════════
# Banner & Help
# ══════════════════════════════════════════════════════════════


def print_banner():
    """打印 Banner — 先播 Logo 动画，再显示标题。"""
    play_intro_animation()
    console.print(Panel(
        Text("""奇天 v1.0

可上九天揽月，可下五洋捉鳖
━━━━━━━━━━━━━━━━━━━━━━━━
一个智能助手 — 读写文件 · 运行代码 · 搜索网络 · 系统工具 · 持久记忆

Planner → Executor → Tool Router → Memory""",
             style="bold #c39bdb", justify="center"),
        border_style="#9b59b6",
        padding=(1, 2),
    ))


def print_help():
    console.print("""
[bold]可用命令:[/bold]
  exit / quit         — 退出
  help / 帮助         — 显示此帮助

  plan / 查看计划     — 显示当前任务计划
  memory / 查看记忆   — 显示记忆摘要
  tools / 查看工具    — 列出所有可用工具
  history / 查看历史  — 显示最近的执行记录
  stream              — 切换到流式模式

[bold]示例:[/bold]
  现在几点?
  计算 3^2 + 4^2
  读取 d:/ai/config.py
  搜索 Python async 最佳实践
  帮我运行 Python 代码 print('hello')
  记住我的名字是 Li
""")


# ══════════════════════════════════════════════════════════════
# Interactive Mode (Standard)
# ══════════════════════════════════════════════════════════════


def interactive_mode(orch: AgentOrchestrator):
    """交互式 REPL (Rich Prompt)。"""
    print_banner()
    console.print(f"\n模型: {config.MODEL_NAME} | 最大步骤: {config.MAX_PLAN_STEPS} | 重试: {config.MAX_RETRIES}")
    console.print("输入 help 查看帮助，输入 exit 退出\n")

    while True:
        try:
            user_input = Prompt.ask("[bold green]你[/bold green]").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n再见！")
            break

        if not user_input:
            continue

        # 命令处理
        result = _handle_command(orch, user_input)
        if result == "exit":
            break
        if result == "handled":
            continue

        # 执行
        try:
            answer = orch.run_chat(user_input)
        except Exception as e:
            answer = f"[错误] 执行失败: {e}"

        console.print(f"\n[bold blue]AI[/bold blue]：{answer}\n")


# ══════════════════════════════════════════════════════════════
# Streaming Interactive Mode
# ══════════════════════════════════════════════════════════════


async def streaming_interactive_mode(orch: AgentOrchestrator):
    """流式交互模式 — Rich Live 实时更新。"""
    print_banner()
    console.print(f"\n模型: {config.MODEL_NAME} | 模式: 流式 | 重试: {config.MAX_RETRIES}")
    console.print("输入 help 查看帮助，输入 exit 退出，输入 standard 切换回标准模式\n")

    while True:
        try:
            user_input = Prompt.ask("[bold green]你[/bold green]").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n再见！")
            break

        if not user_input:
            continue

        # 命令处理
        result = _handle_command(orch, user_input)
        if result == "exit":
            break
        if result == "handled":
            continue
        if result == "switch_standard":
            interactive_mode(orch)
            return

        # 流式执行
        try:
            with AgentDisplay() as display:
                async for event in orch.run_stream(user_input):
                    display.render(event)
            console.print()  # 换行
        except Exception as e:
            console.print(f"\n[red]错误[/red]: {e}\n")


async def streaming_single_query(orch: AgentOrchestrator, query: str):
    """流式单次查询。"""
    try:
        with AgentDisplay() as display:
            async for event in orch.run_stream(query):
                display.render(event)
        console.print()
    except Exception as e:
        console.print(f"❌ 执行失败: {e}")


# ══════════════════════════════════════════════════════════════
# Command Handler (shared)
# ══════════════════════════════════════════════════════════════


def _handle_command(orch: AgentOrchestrator, user_input: str) -> str | None:
    """
    处理内置命令。返回值:
      "exit"        — 退出
      "handled"     — 命令已处理
      "switch_standard" — 切换到标准模式
      None          — 非命令，继续执行
    """
    cmd = user_input.lower().strip()

    if cmd in {"exit", "quit", "退出"}:
        console.print("再见！")
        return "exit"

    if cmd in {"help", "帮助"}:
        print_help()
        return "handled"

    if cmd in {"plan", "查看计划"}:
        if orch.current_plan:
            console.print(f"\n当前计划:\n{orch.current_plan.to_json()}")
        else:
            console.print("暂无活动计划。")
        return "handled"

    if cmd in {"memory", "查看记忆", "记忆"}:
        console.print(f"\n{orch.get_memory_summary()}")
        return "handled"

    if cmd in {"tools", "查看工具", "工具"}:
        console.print(f"\n可用工具 ({len(orch.registry)} 个):")
        for t in orch.registry:
            s = t.schema
            ro = "(r)" if s.is_readonly else "(w)"
            app = "[lock]" if s.requires_approval else ""
            console.print(f"  {app}[{s.category}] {s.name} {ro}: {s.description[:80]}")
        return "handled"

    if cmd in {"history", "查看历史", "查看执行历史"}:
        summary = orch.get_execution_summary()
        console.print(f"\n执行摘要:\n{json.dumps(summary, ensure_ascii=False, indent=2)}")
        last = orch.get_last_execution()
        if last:
            console.print(f"\n最近执行: {last.get('goal', '')[:80]}...")
        return "handled"

    if cmd in {"stream", "流式"}:
        console.print("[yellow]已在流式模式。输入 'standard' 切换回标准模式。[/yellow]")
        return "handled"

    if cmd in {"standard", "标准"}:
        return "switch_standard"

    return None


# ══════════════════════════════════════════════════════════════
# Setup
# ══════════════════════════════════════════════════════════════


def setup_wizard():
    """首次运行向导 — 输入并保存 API Key。"""
    console.print(Panel(
        Text("首次设置 — API Key\n\n"
             "你的 API Key 在哪找:\n"
             "  • DeepSeek:  https://platform.deepseek.com/api_keys\n"
             "  • OpenAI:    https://platform.openai.com/api-keys",
             style="yellow", justify="center"),
        border_style="yellow",
    ))

    while True:
        key = Prompt.ask("请输入你的 API Key").strip()
        if key:
            break
        console.print("[red]Key 不能为空，请重新输入。[/red]")

    console.print(f"\n将保存到: {config.API_KEY_FILE}")
    config.API_KEY_FILE.write_text(key, "utf-8")
    console.print("[green]API Key 已保存！[/green] 下次启动无需再次输入。\n")
    return key


# ══════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════


def parse_args():
    parser = argparse.ArgumentParser(
        description="奇天 v1.0 — 4-layer AI Agent Framework",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python main.py                       交互模式
  python main.py "现在几点?"           单次查询
  python main.py --stream              流式交互模式
  python main.py --stream "现在几点?"  流式单次查询
  python main.py --model gpt-4        使用 GPT-4
        """,
    )
    parser.add_argument(
        "query",
        nargs="*",
        help="查询文本 (不提供则进入交互模式)",
    )
    parser.add_argument(
        "--stream", "-s",
        action="store_true",
        default=False,
        help="启用流式模式 (Rich Live 实时显示)",
    )
    parser.add_argument(
        "--no-stream",
        action="store_true",
        default=False,
        help="禁用流式模式 (默认)",
    )
    parser.add_argument(
        "--model", "-m",
        type=str,
        default=None,
        help="指定模型 (覆盖配置中的 MODEL_NAME)",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    # 模型覆盖
    if args.model:
        config.MODEL_NAME = args.model
        # 同步更新全局 settings
        from agent.settings import get_settings
        settings = get_settings()
        settings.model_name = args.model

    # 检查 API Key
    if not config.API_KEY:
        key = setup_wizard()
        config.API_KEY = key

    orch = AgentOrchestrator()

    try:
        orch.initialize()
    except RuntimeError as e:
        console.print(f"[red]初始化失败[/red]: {e}")
        console.print("请检查 API Key 是否正确，或重新运行程序。")
        sys.exit(1)

    query = " ".join(args.query) if args.query else ""

    # 流式模式
    if args.stream:
        if query:
            asyncio.run(streaming_single_query(orch, query))
        else:
            asyncio.run(streaming_interactive_mode(orch))
        return

    # 标准模式
    if query:
        answer = orch.run_chat(query)
        console.print(answer)
    else:
        interactive_mode(orch)


if __name__ == "__main__":
    main()
