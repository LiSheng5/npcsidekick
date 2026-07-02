#!/usr/bin/env python3
"""
Agent v3.0 演示脚本 — 展示 4 层架构的完整功能。

运行:
  python demo_v3.py
"""
import json
import sys
import io

# Fix Windows GBK encoding for emoji
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from agent.orchestrator import AgentOrchestrator
from agent.tools.schema import ToolCall, ToolResultStatus


def test_tool_call_format():
    """展示统一工具调用格式。"""
    print("\n" + "=" * 60)
    print("📐 统一工具调用格式演示")
    print("=" * 60)

    # 构造一个 ToolCall
    call = ToolCall(
        tool="read_file",
        input={"path": "d:/ai/config.py", "limit": 20},
        reason="需要读取配置文件来了解当前系统参数",
    )
    print(f"\n输入格式 (ToolCall):\n  {call.to_json()}")

    # 模拟执行
    from agent.tools.registry import get_registry
    from agent.tools.builtin.file_tools import ReadFileTool
    registry = get_registry()
    registry.register(ReadFileTool())

    from agent.tools.router import ToolRouter
    router = ToolRouter(registry)

    result = router.dispatch(call)
    print(f"\n输出格式 (ToolResult):\n  status={result.status.value}")
    print(f"  ok={result.ok}")
    print(f"  duration_ms={result.duration_ms:.1f}")
    if result.ok and result.data:
        content = result.data.get("content", "")[:100]
        print(f"  data.content (前100字符): {content}...")
    elif result.error:
        print(f"  error: {result.error}")


def test_planner():
    """展示 Planner 生成 TaskPlan。"""
    print("\n" + "=" * 60)
    print("🧠 Planner — 任务分解演示")
    print("=" * 60)

    # 初始化
    from agent.llm.client import LLMClient
    from agent.memory.memory_manager import MemoryManager
    from agent.planner.planner import Planner

    try:
        llm = LLMClient()
        memory = MemoryManager()
        planner = Planner(llm, memory)

        # 测试: 简单查询
        print("\n▶ 测试: '现在几点?'")
        plan = planner.plan("现在几点?")
        print(f"  目标: {plan.goal}")
        print(f"  步骤数: {len(plan.steps)}")
        for s in plan.steps:
            print(f"  Step {s.step_id}: {s.description}")
            print(f"    工具: {s.tool}")
            print(f"    成功标准: {s.success_criteria}")

        # 测试: 复杂任务
        print("\n▶ 测试: '找出 ai.py 中所有的 print 语句并列出'")
        plan = planner.plan("找出 ai.py 中所有的 print 语句并列出")
        print(f"  目标: {plan.goal}")
        print(f"  步骤数: {len(plan.steps)}")
        for s in plan.steps:
            print(f"  Step {s.step_id}: {s.description}")
            print(f"    工具: {s.tool}")
            print(f"    依赖: {s.depends_on}")
            print(f"    备选方案: {s.fallback}")

    except RuntimeError as e:
        print(f"  ⚠️ 跳过 (需要 API Key): {e}")


def test_tool_router():
    """展示工具路由器。"""
    print("\n" + "=" * 60)
    print("🔀 Tool Router — 动态工具选择演示")
    print("=" * 60)

    from agent.tools.registry import get_registry
    registry = get_registry()

    # 注册工具
    from agent.tools.builtin.file_tools import ReadFileTool, WriteFileTool, ListDirTool
    from agent.tools.builtin.system_tools import GetTimeTool, CalculatorTool
    from agent.tools.builtin.web_tools import WebSearchTool
    registry.register_many([
        ReadFileTool(), WriteFileTool(), ListDirTool(),
        GetTimeTool(), CalculatorTool(),
        WebSearchTool(),
    ])

    from agent.tools.router import ToolRouter
    router = ToolRouter(registry)

    # 推荐工具
    print("\n▶ 任务: '读取 config.py 文件'")
    tools = router.recommend_tools("读取 config.py 文件", max_results=3)
    for t in tools:
        print(f"  推荐: {t.name} [{t.category}] — {t.description[:60]}")

    print("\n▶ 任务: '搜索 Python 的最佳实践'")
    tools = router.recommend_tools("搜索 Python 的最佳实践", max_results=3)
    for t in tools:
        print(f"  推荐: {t.name} [{t.category}] — {t.description[:60]}")

    print("\n▶ 任务: '计算 3^2 + 4^2'")
    tools = router.recommend_tools("计算 3^2 + 4^2", max_results=3)
    for t in tools:
        print(f"  推荐: {t.name} [{t.category}] — {t.description[:60]}")

    # 按类别查找
    print("\n▶ 所有文件类工具:")
    for t in registry.find_by_category("file"):
        print(f"  {t.name} — {t.schema.description[:60]}")

    print(f"\n▶ 所有工具 (共 {len(registry)} 个):")
    for name in registry.list_names():
        print(f"  - {name}")


def test_memory_layer():
    """展示记忆层。"""
    print("\n" + "=" * 60)
    print("💾 Memory Layer — 记忆系统演示")
    print("=" * 60)

    from agent.memory.memory_manager import MemoryManager
    mm = MemoryManager()

    # 写入
    mm.add_message("user", "我叫 Li")
    mm.add_message("assistant", "你好 Li!")
    mm.remember("用户名叫 Li", category="identity")
    mm.remember("用户喜欢 Python", category="preference")
    mm.learn("用户在下午更活跃", source="observation")

    # 读取
    print(f"\n短时记忆: {len(mm.short_term)} 条消息")
    print(f"长时记忆: {len(mm.long_term)} 条事实 + 规律")

    # 检索
    result = mm.retrieve("Li")
    print(f"\n搜索 'Li':")
    print(f"  短时匹配: {len(result['short_term'])} 条")
    print(f"  事实匹配: {len(result['long_term']['facts'])} 条")

    # 为规划检索
    context = mm.retrieve_for_planning("用户想提高 Python 技能")
    print(f"\n为规划检索上下文:\n{context[:500]}...")

    # 摘要
    print(f"\n记忆摘要:\n{mm.summarize()}")

    # 清理
    mm.clear()


def test_full_orchestrator():
    """完整编排器测试。"""
    print("\n" + "=" * 60)
    print("🚀 完整编排器测试")
    print("=" * 60)

    try:
        orch = AgentOrchestrator()
        orch.initialize()

        # 测试简单查询
        print("\n▶ 测试 1: '现在几点?'")
        answer = orch.run_chat("现在几点?")
        print(f"  答案: {answer}")

        # 测试计算
        print("\n▶ 测试 2: '计算 3+5'")
        answer = orch.run_chat("计算 3+5")
        print(f"  答案: {answer}")

        # 查看记忆
        print(f"\n▶ 记忆摘要:\n{orch.get_memory_summary()}")

        # 查看工具
        print(f"\n▶ 已注册工具: {len(orch.registry)} 个")
        for name in sorted(orch.registry.list_names()):
            print(f"  - {name}")

    except RuntimeError as e:
        print(f"  ⚠️ 跳过 (需要 API Key): {e}")


def main():
    print("=" * 60)
    print("奇天 v1.0 — 架构验证演示")
    print("=" * 60)

    # 1. 工具调用格式
    test_tool_call_format()

    # 2. 工具路由器
    test_tool_router()

    # 3. 记忆层
    test_memory_layer()

    # 4. Planner (需要 API Key)
    test_planner()

    # 5. 完整编排器 (需要 API Key)
    test_full_orchestrator()

    print("\n" + "=" * 60)
    print("✅ 演示完成")
    print("=" * 60)


if __name__ == "__main__":
    main()
