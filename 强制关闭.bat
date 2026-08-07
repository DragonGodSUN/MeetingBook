@echo off
rem ============================================================
rem  关闭 MeetingBook Web 界面（双击运行）
rem ============================================================
chcp 65001 >nul
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop_webui.ps1"
echo.
pause
