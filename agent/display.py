"""
AgentDisplay — Rich Live 流式渲染器。

消费 StreamEvent 事件流，实时更新终端界面。

使用方式:
  display = AgentDisplay()
  with display:
      async for event in orch.run_stream("问题"):
          display.render(event)
"""

from __future__ import annotations

import time
from typing import Optional

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
from rich.table import Table
from rich.text import Text
from rich import box

from agent.llm.types import StreamEvent, StreamEventType


class AgentDisplay:
    """Rich Live 终端 UI — 流式 Agent 输出。"""

    def __init__(self, theme: str = "dark"):
        self.console = Console()
        self._live: Optional[Live] = None
        self._layout = Layout()
        self._start_time = time.time()

        # 累积状态
        self._goal: str = ""
        self._steps_total: int = 0
        self._steps_done: int = 0
        self._steps_failed: int = 0
        self._current_step: str = ""
        self._current_tool: str = ""
        self._output_text: str = ""
        self._phase: str = "初始化..."
        self._answer: str = ""

        # 主题
        self._theme = theme
        self._border_style = "cyan" if theme == "dark" else "blue"

        self._build_layout()

    # ── Context Manager ──────────────────────────────────

    def __enter__(self) -> "AgentDisplay":
        self._live = Live(
            self._layout,
            console=self.console,
            refresh_per_second=10,
            screen=True,
        )
        self._live.__enter__()
        self._start_time = time.time()
        return self

    def __exit__(self, *args):
        if self._live:
            self._live.__exit__(*args)
            self._live = None

    # ── Public API ───────────────────────────────────────

    def render(self, event: StreamEvent) -> None:
        """处理 StreamEvent 并更新显示。"""
        handler = getattr(self, f"_on_{event.type.value}", None)
        if handler:
            handler(event)

        if self._live:
            self._build_layout()
            self._live.update(self._layout)

    def close(self) -> None:
        """手动关闭 Live 显示。"""
        if self._live:
            self._live.stop()
            self._live = None

    # ── Event Handlers ───────────────────────────────────

    def _on_thinking(self, event: StreamEvent) -> None:
        self._phase = "思考中..."
        self._goal = event.message

    def _on_plan_ready(self, event: StreamEvent) -> None:
        self._phase = "计划就绪"
        self._goal = event.data.get("goal", self._goal)
        self._steps_total = event.data.get("steps_count", 0)

    def _on_step_start(self, event: StreamEvent) -> None:
        self._phase = "执行中"
        step_id = event.data.get("step_id", "?")
        self._current_step = f"步骤 {step_id}: {event.data.get('description', '')}"
        self._current_tool = event.data.get("tool", "")

    def _on_step_progress(self, event: StreamEvent) -> None:
        self._current_step = event.message

    def _on_tool_call(self, event: StreamEvent) -> None:
        self._current_tool = event.data.get("tool_name", "")

    def _on_tool_result(self, event: StreamEvent) -> None:
        ok = event.data.get("ok", False)
        if ok:
            self._steps_done += 1
        else:
            self._steps_failed += 1

    def _on_step_done(self, event: StreamEvent) -> None:
        self._current_tool = ""

    def _on_reflection(self, event: StreamEvent) -> None:
        decision = event.data.get("decision", "")
        if decision in ("retry", "replan"):
            self._phase = f"🔄 {decision}"

    def _on_text_delta(self, event: StreamEvent) -> None:
        content = event.data.get("content", "")
        self._output_text += content

    def _on_synthesis(self, event: StreamEvent) -> None:
        self._phase = "生成答案..."

    def _on_done(self, event: StreamEvent) -> None:
        self._phase = "✅ 完成"
        self._answer = event.data.get("answer", "")

    def _on_error(self, event: StreamEvent) -> None:
        self._phase = f"❌ 错误"
        self._output_text += f"\n[red]错误: {event.message}[/red]"

    # ── Layout ───────────────────────────────────────────

    def _build_layout(self) -> None:
        self._layout.split(
            Layout(self._make_header(), name="header", size=3),
            Layout(self._make_body(), name="body"),
            Layout(self._make_footer(), name="footer", size=3),
        )

    def _make_header(self) -> Panel:
        elapsed = time.time() - self._start_time
        status_line = (
            f"[bold white]NPCSidekick v3.1[/bold white]  |  "
            f"{self._phase}  |  "
            f"耗时: {elapsed:.1f}s  |  "
            f"步骤: {self._steps_done}/{self._steps_total}"
            if self._steps_total
            else f"耗时: {elapsed:.1f}s"
        )
        if self._steps_failed:
            status_line += f"  |  [red]失败: {self._steps_failed}[/red]"

        return Panel(
            Text(status_line, style="bold"),
            style=self._border_style,
            box=box.ROUNDED,
        )

    def _make_body(self) -> Layout:
        body = Layout()
        body.split_row(
            Layout(self._make_step_panel(), name="left", ratio=2),
            Layout(self._make_output_panel(), name="right", ratio=3),
        )
        return body

    def _make_step_panel(self) -> Panel:
        content = ""

        if self._goal:
            content += f"[bold cyan]目标:[/bold cyan]\n  {self._goal}\n\n"

        if self._current_step:
            content += f"[bold yellow]当前:[/bold yellow]\n  {self._current_step}"
            if self._current_tool:
                content += f"\n  工具: {self._current_tool}"
            content += "\n\n"

        if self._steps_total > 0:
            content += f"[bold green]进度:[/bold green]\n"
            done_bar = "█" * self._steps_done
            pending_bar = "░" * (self._steps_total - self._steps_done - self._steps_failed)
            failed_bar = "✗" * self._steps_failed
            content += f"  [{done_bar}{failed_bar}{pending_bar}] {self._steps_done + self._steps_failed}/{self._steps_total}"

        if not content:
            content = "[dim]等待任务...[/dim]"

        return Panel(
            content,
            title="计划",
            border_style=self._border_style,
            box=box.ROUNDED,
        )

    def _make_output_panel(self) -> Panel:
        if self._output_text:
            # 截断过长的输出
            display_text = self._output_text
            if len(display_text) > 2000:
                display_text = "...(earlier output truncated)\n" + display_text[-1800:]
            content = Text(display_text, style="white")
        elif self._answer:
            content = Text(self._answer, style="green")
        else:
            content = Text("[dim]等待输出...[/dim]", style="dim")

        return Panel(
            content,
            title="输出",
            border_style=self._border_style,
            box=box.ROUNDED,
        )

    def _make_footer(self) -> Panel:
        hints = "[dim]Ctrl+C 中断 | 流式模式[/dim]"
        return Panel(Text(hints), style="dim", box=box.MINIMAL)
