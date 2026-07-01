#!/usr/bin/env python3
"""
Claude 级 Agent 演示脚本
展示新架构的完整功能：多步推理、决策日志、执行状态追踪
"""

import json
import ai

def demo_agent_capabilities():
    """演示 Agent 的核心能力"""
    
    print("=" * 70)
    print("🚀 Claude 级 Agent v2.0 功能演示")
    print("=" * 70)
    
    # 初始化
    client = ai.get_client()
    memory = ai.load_memory()
    
    # 测试场景
    test_queries = [
        ("现在几点?", "简单查询 - 测试工具调用"),
        ("记住我叫张三", "记忆功能 - 测试 remember_fact"),
        ("计算 2+2*3", "计算表达式 - 测试 calculator"),
    ]
    
    for query, description in test_queries:
        print(f"\n{'─' * 70}")
        print(f"📌 测试: {description}")
        print(f"输入: {query}")
        print(f"{'─' * 70}\n")
        
        try:
            # 执行 Agent
            answer = ai.chat_with_agent(client, memory, query)
            
            # 显示答案
            print(f"💬 答案: {answer}\n")
            
            # 显示执行状态（从内存日志中提取）
            exec_log = memory.get("execution_log", [])
            if exec_log:
                last_exec = exec_log[-1]
                print("📊 执行状态:")
                print(f"  - 步数: {last_exec.get('step')}")
                print(f"  - 思考步骤: {len(last_exec.get('thoughts', []))} 步")
                print(f"  - 工具调用: {len(last_exec.get('tool_calls', []))} 次")
                
                # 显示工具调用详情
                if last_exec.get('tool_calls'):
                    print("  - 调用的工具:")
                    for tc in last_exec['tool_calls']:
                        print(f"    • {tc['tool']}: {json.dumps(tc['arguments'], ensure_ascii=False)}")
                
                # 显示决策日志
                if last_exec.get('decision_log'):
                    print("  - 决策过程:")
                    for d in last_exec['decision_log']:
                        print(f"    • [{d['decision']}]")
            
            # 记录到历史
            ai.append_history(memory, "user", query)
            ai.append_history(memory, "assistant", answer)
            
        except Exception as e:
            print(f"❌ 错误: {e}\n")
    
    # 显示最终统计
    print(f"\n{'=' * 70}")
    print("📈 会话统计")
    print(f"{'=' * 70}")
    print(f"总执行次数: {len(memory.get('execution_log', []))}")
    print(f"总历史记录: {len(memory.get('history', []))}")
    print(f"已记忆事实: {len(memory.get('facts', []))}")
    
    # 显示记忆内容
    if memory.get("facts"):
        print("\n🧠 长期记忆:")
        for fact in memory['facts'][-3:]:  # 最后 3 条
            print(f"  • {fact}")
    
    # 保存内存
    ai.save_memory(memory)
    print("\n✅ 内存已保存")


if __name__ == "__main__":
    demo_agent_capabilities()
