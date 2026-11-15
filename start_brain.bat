@echo off
chcp 65001 > nul
title NPCSidekick Brain - port 8000
echo ================================================
echo  NPCSidekick brain service
echo  http://127.0.0.1:8000
echo.
echo  Keep this window open while playing.
echo  Close this window to stop the service.
echo ================================================
echo.
"C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe" -m uvicorn server:app --app-dir "D:/NPCSidekick_v4" --host 127.0.0.1 --port 8000 --log-level info
echo.
echo Service stopped. Press any key to close.
pause > nul
