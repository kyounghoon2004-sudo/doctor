@echo off
REM ============================================================
REM  YouTube Shorts Dashboard - double-click launcher
REM  Runs the venv Python on dashboard.py with absolute paths,
REM  so it works no matter where it's launched from and avoids
REM  the PowerShell execution-policy issue (this is cmd, not PS).
REM ============================================================
cd /d "%~dp0"
echo Starting the Shorts dashboard...
echo If your browser does not open automatically, go to: http://127.0.0.1:5000
echo (Close this window or press Ctrl+C to stop the dashboard.)
echo.
".venv\Scripts\python.exe" "dashboard.py" %*
echo.
echo Dashboard stopped.
pause
