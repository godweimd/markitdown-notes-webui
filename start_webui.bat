@echo off
chcp 65001 >nul
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" "webui\server.py" %*
) else (
    python "webui\server.py" %*
)

pause
