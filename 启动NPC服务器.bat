@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   NPCSidekick 大脑服务器（苍/阿黎）
echo   启动后保持窗口开着，玩完再关
echo ============================================
python -m npc.server --adapter paleolithic --no-browser
pause
