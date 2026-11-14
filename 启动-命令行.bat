@echo off
rem ============================================================
rem  Law Agent - CLI launcher
rem  Keep this file pure ASCII: cmd parses batch by byte offset, and
rem  Chinese text under chcp 65001 makes it drift, so fragments of
rem  echo lines get executed as commands. See the web launcher header.
rem ============================================================
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
title Law Agent - CLI

set "PY="
where python >nul 2>nul
if not errorlevel 1 set "PY=python"

if not defined PY (
    where py >nul 2>nul
    if not errorlevel 1 set "PY=py -3"
)

if not defined PY (
    if exist "D:\anaconda\python.exe" set "PY=D:\anaconda\python.exe"
)

if not defined PY (
    echo   [ERROR] Python not found.
    echo.
    echo   Please install Python 3.10 or newer:
    echo       https://www.python.org/downloads/
    echo   During setup, tick "Add python.exe to PATH" on the first page.
    echo   Then close this window and double-click this file again.
    echo.
    pause
    exit /b 1
)

rem The code uses "int ^| None" (PEP 604), which needs 3.10+.
%PY% -c "import sys;raise SystemExit(0 if sys.version_info>=(3,10) else 9)" >nul 2>nul
if errorlevel 1 (
    echo   [ERROR] Python is too old. This project needs 3.10 or newer.
    echo   Found:
    %PY% -c "import sys;print('      Python '+sys.version.split()[0]+'   '+sys.executable)"
    echo.
    echo   Please upgrade: https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

%PY% -c "import pydantic" >nul 2>nul
if errorlevel 1 (
    echo.
    echo   [SETUP] First run: installing dependencies. Needs internet...
    %PY% -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo   [ERROR] Dependency install failed. Check your network, then run:
        echo       %PY% -m pip install -r requirements.txt
        echo   Behind a slow link, use a mirror:
        echo       %PY% -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
        echo.
        pause
        exit /b 1
    )
)

if not exist "data\seed_statutes.jsonl" (
    echo   [SETUP] First run: building the seed corpus...
    %PY% scripts\make_seed.py
)

echo.
echo   Type a question, or /trace to see the steps, /quit to exit.
echo.
%PY% -m app.cli %*
pause
