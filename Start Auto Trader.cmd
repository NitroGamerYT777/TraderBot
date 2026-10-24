@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
    if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -c "import fastapi, uvicorn, httpx, websockets, keyring" >nul 2>&1
if errorlevel 1 (
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 goto failed
)
echo Auto Trader: http://127.0.0.1:8000
echo Keep this window open. Press Ctrl+C to stop.
".venv\Scripts\python.exe" run.py --open-browser
if errorlevel 1 goto failed
exit /b 0
:failed
echo Could not start Auto Trader. Check Python, internet, or port 8000.
pause
exit /b 1
