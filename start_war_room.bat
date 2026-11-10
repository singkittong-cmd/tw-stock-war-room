@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PATH=C:\Users\singk\anaconda3;C:\Users\singk\anaconda3\Scripts;C:\Users\singk\anaconda3\Library\bin;%USERPROFILE%\.local\bin;%PATH%"
set "PYTHONUTF8=1"
echo [INFO] Starting Streamlit War Room Dashboard (headless server, SimRoomDaemon runs in this process)...
echo [INFO] Keep the "TW-Stock-Dashboard" window open: closing it stops the background auto-trading daemon.
start "TW-Stock-Dashboard" "C:\Users\singk\anaconda3\python.exe" -m streamlit run dashboard.py --server.headless true --server.runOnSave false --browser.gatherUsageStats false
rem headless mode does not open a browser: open the page once, the first page load starts the background daemon
timeout /t 5 /nobreak >nul
start "" "http://localhost:8501"
echo [INFO] Launching Claude Code...
claude -c || claude
pause
