@echo off
REM First-time setup (Windows): create .venv here and install dependencies.
REM ASCII only: cmd.exe mis-parses batch files that mix chcp with multibyte text.
cd /d "%~dp0"

set "PY="
where py >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if not defined PY (
  where python >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  echo Python 3.9+ was not found. Install it from https://www.python.org/ and retry.
  pause
  exit /b 1
)

%PY% -m venv .venv || goto :fail
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul || goto :fail
".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :fail
".venv\Scripts\python.exe" zmkproto_decode.py || goto :fail
echo.
echo Setup complete. Run "zmkbak.cmd doctor" to check the connection.
pause
exit /b 0

:fail
echo.
echo Setup failed. Check that Python 3.9 or newer is installed.
pause
exit /b 1
