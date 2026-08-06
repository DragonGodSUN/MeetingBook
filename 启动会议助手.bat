@echo off
rem ============================================================
rem  MeetingBook one-click launcher (double-click to run)
rem  Pass-through args: start.bat search "budget"
rem ============================================================
chcp 65001 >nul
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_meetingbook.ps1" %*
echo.
pause
