@echo off
setlocal
cd /d "%~dp0"
set "SEATIDE_ENV=dev"
echo [seatide-bot] starting (dev)...
"%~dp0.venv\Scripts\python.exe" bot.py
echo.
echo [seatide-bot] exited, code=%errorlevel%
pause
