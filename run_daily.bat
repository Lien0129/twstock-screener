@echo off
rem Taiwan stock system - daily update
rem Called by Windows Task Scheduler. Double-click to test manually.
rem Comments kept in ASCII on purpose: Chinese in .bat breaks under cp950.

chcp 65001 > nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
cd /d "%~dp0"

if not exist logs mkdir logs

rem Date for the log filename.
rem WMIC was removed from recent Windows builds, so the old
rem   for /f "tokens=2 delims==" %%I in ('wmic os get localdatetime /value')
rem produced no output, DT stayed undefined, and the substring expansion
rem %%DT:~0,8%% fell through as literal text -> "logs\daily_~0,8.log".
rem Every run then appended to that one file. PowerShell works everywhere
rem that still runs this script; "date" is skipped on purpose because its
rem format follows the machine locale and can contain "/".
set "DT="
for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd" 2^>nul') do set "DT=%%I"
if not defined DT set "DT=nodate"
set "LOGFILE=logs\daily_%DT%.log"

echo ============================================ >> "%LOGFILE%" 2>&1
echo START %date% %time% >> "%LOGFILE%" 2>&1

python daily_update.py >> "%LOGFILE%" 2>&1
set RC=%errorlevel%

echo END %date% %time% exit=%RC% >> "%LOGFILE%" 2>&1

if %RC% neq 0 (
  echo [FAILED] see %LOGFILE%
) else (
  echo [OK] update finished
)

exit /b %RC%
