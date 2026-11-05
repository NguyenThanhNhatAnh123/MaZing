@echo off
setlocal
cd /d "%~dp0"
call "%~dp0_python.bat" || exit /b 1
"%PY%" dashboard.py %*
echo.
echo Dashboard da dung (ma %errorlevel%).
pause
