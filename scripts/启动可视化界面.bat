@echo off
rem ============================================================
rem  MeetingBook Web UI one-click launcher (double-click to run)
rem  Opens the visual interface in your default browser.
rem ============================================================
chcp 65001 >nul
cd /d "%~dp0.."
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_meetingbook.ps1" webui
echo.
pause
