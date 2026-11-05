@echo off
setlocal
cd /d "%~dp0"
call "%~dp0_python.bat" || exit /b 1
"%PY%" -c "from lib import mayzing; import sys; ok = mayzing.login_interactive(); mayzing.shutdown(); sys.exit(0 if ok else 1)"
pause
