@echo off
rem ============================================================
rem  MeetingBook 会议助手一键启动（双击进入交互菜单）
rem  也可带参数直接执行：启动会议助手.bat search "预算"
rem ============================================================
chcp 65001 >nul
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_meetingbook.ps1" %*
echo.
pause
