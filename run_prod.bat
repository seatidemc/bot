@echo off
setlocal
cd /d "%~dp0"
set "SEATIDE_ENV=prod"
echo [seatide-bot] starting (prod)...
"%~dp0.venv\Scripts\python.exe" bot.py
echo.
echo [seatide-bot] exited, code=%errorlevel%
pause
