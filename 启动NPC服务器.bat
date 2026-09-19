@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   NPCSidekick 大脑服务器（苍/阿黎）
echo   启动后保持窗口开着，玩完再关
echo ============================================
rem ── §17 子代理(2026-08-24): B2 编译 / A 审查 LLM 化, 0=关 ──
set NPC_SUBAGENT_B2=1
rem ── 决策层可得性探针(2026-09-19): 候选过滤掉"当前采不到"的活, 免每 5 tick 空撞一次 + 不堆假失败, 0=关 ──
set NPC_AVAILABILITY=1
python -m npc.server --adapter paleolithic --no-browser
pause
