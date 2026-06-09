@echo off
setlocal
cd /d "%~dp0"

REM Uses python from PATH. To use a specific interpreter, set MEETFLOW_PY:
REM   set "MEETFLOW_PY=C:\Python311\python.exe"
if defined MEETFLOW_PY (
    set "PY=%MEETFLOW_PY%"
) else (
    set "PY=python"
)

start "MeetFlow" /min "%PY%" -m meetflow listen %*
endlocal
