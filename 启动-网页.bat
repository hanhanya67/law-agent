@echo off
rem ============================================================
rem  Law Agent - Web launcher
rem
rem  IMPORTANT: this file must stay pure ASCII.
rem  cmd parses a batch file by BYTE OFFSET. Under chcp 65001 a
rem  UTF-8 Chinese line makes the offset drift, and cmd then starts
rem  executing fragments of the echo text as commands. Symptom:
rem  lines like "'-..." is not recognized as an internal or
rem  external command" mixed with the real output.
rem  Chinese help text lives in notice.txt and is printed with
rem  `type`, which does not go through the batch parser at all.
rem ============================================================
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
title Law Agent - Web

rem ---------- port ----------
rem Defined ONCE. The previous version hard-coded 8000 in the uvicorn
rem command AND in the two URLs, then told the user to change both by
rem hand - two places that drift apart as soon as one is edited.
set "PORT=8000"

echo.
echo   ==========================================
echo     Law Agent  -  Web UI
echo   ==========================================
echo.

rem ---------- 1. locate python ----------
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

rem ---------- 2. version gate ----------
rem The code uses "int ^| None" (PEP 604), which needs 3.10+. On 3.8/3.9
rem it fails at import time with a syntax error that looks like broken code.
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

echo   Python:
%PY% -c "import sys;print('      Python '+sys.version.split()[0]+'   '+sys.executable)"

rem ---------- 3. dependencies ----------
%PY% -c "import pydantic, fastapi, uvicorn" >nul 2>nul
if errorlevel 1 (
    echo.
    echo   [1/3] First run: installing dependencies. Needs internet, 1-2 min...
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

rem ---------- 4. corpus ----------
if not exist "data\seed_statutes.jsonl" (
    echo.
    echo   [2/3] First run: building the seed corpus...
    %PY% scripts\make_seed.py
)

rem ---------- 5. full corpus gate ----------
rem The seed corpus is 17 articles: enough to boot, far too few to answer.
rem Without this gate the page opens normally and most questions just come
rem back as "not in the library" - which reads like a broken product
rem rather than a missing data file. data\raw\ ships with the project,
rem so rebuilding is a local, offline step.
if not exist "data\statutes.jsonl" (
    echo.
    echo   [WARN] data\statutes.jsonl NOT found.
    echo          Only the 17-article SEED corpus is available.
    echo          Most questions will answer "not in the library".
    echo.
    echo          Fix it - either:
    echo            1^) put statutes.jsonl back into data\    [safest]
    echo            2^) rebuild it from data\raw\ ^(offline^):
    echo                 %PY% scripts\import_law_text.py
    echo                 NOTE: this rebuilds only the 8 laws kept in
    echo                 data\raw\ ^(2060 articles^). The 6 workplace-injury
    echo                 articles are NOT in data\raw\, so a plain rebuild
    echo                 would drop them. The script now refuses to
    echo                 overwrite when the result is smaller, and says so.
    echo.
    echo          The page will also show a warning banner until this is fixed.
    echo.
)

rem ---------- 6. config ----------
if not exist ".env" (
    if exist ".env.example" (
        copy /y ".env.example" ".env" >nul
        echo.
        echo   [NOTE] Created .env from .env.example.
        if exist "notice.txt" type "notice.txt"
    )
)

rem ---------- 7. port preflight ----------
rem Why this exists: uvicorn on an occupied port dies in well under a
rem second, but the browser was opened BEFORE it - so the user lands on
rem whatever was already listening. If that happens to be an older copy
rem of this same app, the page works fine and answers from a stale
rem corpus. Symptom: "the page says 2060, the docs say 2066", which
rem gets reported as a data bug and costs hours.
set "OCCUPIED="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:":%PORT% .*LISTENING"') do set "OCCUPIED=%%p"
if defined OCCUPIED (
    echo.
    echo   [ERROR] Port %PORT% is already in use by PID %OCCUPIED%.
    echo.
    echo   Nothing was started and no browser was opened: a page on
    echo   that port belongs to some OTHER program, not this one.
    echo.
    echo   Either close that program, or change the PORT line near
    echo   the top of this file. The URLs below follow it.
    echo.
    pause
    exit /b 1
)

rem ---------- 8. start ----------
echo.
echo   [3/3] Starting the service...
echo.
echo     Web UI    http://127.0.0.1:%PORT%
echo     Admin     http://127.0.0.1:%PORT%/admin
echo.
echo     On the first start an admin account is created and its one-time
echo     password is printed below. It is shown once only - log in and
echo     change it right away.
echo.
echo     If the browser says "cannot reach this site", wait until
echo     "Uvicorn running" shows up below, then refresh once.
echo     Close this window to stop the service.
echo.
start "" http://127.0.0.1:%PORT%
%PY% -m uvicorn app.server:app --host 127.0.0.1 --port %PORT%
echo.
echo   Service stopped. Port %PORT% was free when this window started,
echo   so a startup failure is something else - scroll up for the
echo   traceback rather than guessing about ports.
echo.
pause
