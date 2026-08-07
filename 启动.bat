@echo off
rem ============================================================
rem  MeetingBook Web UI 一键启动（双击运行，自动打开浏览器）
rem  数据目录与 Key 见 .env；关闭请用「强制关闭.bat」
rem ============================================================
chcp 65001 >nul
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_meetingbook.ps1" webui
echo.
pause
