@echo off
rem ============================================================
rem  Stop MeetingBook Web UI (double-click to run)
rem  Kills the python process serving the visual interface.
rem ============================================================
chcp 65001 >nul
cd /d "%~dp0.."
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop_webui.ps1"
echo.
pause
