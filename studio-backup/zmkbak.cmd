@echo off
REM ASCII only: cmd.exe mis-parses batch files that mix chcp with multibyte text.
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo Run setup.cmd first.
  exit /b 1
)
"%~dp0.venv\Scripts\python.exe" "%~dp0zmkbak.py" %*
