@echo off
setlocal
cd /d "%~dp0"
echo === mayzing-bulk-upload: cai dat ===

where py >nul 2>nul && (set "BASEPY=py -3") || (set "BASEPY=python")
%BASEPY% --version >nul 2>nul
if errorlevel 1 (
  echo Khong tim thay Python. Cai Python 3.11+ tu https://www.python.org/downloads/ ^(tick "Add Python to PATH"^).
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Tao moi truong .venv ...
  %BASEPY% -m venv .venv || goto :fail
)
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :fail

if not exist ".env" copy ".env.example" ".env" >nul && echo Da tao .env - mo file nay dan ANTHROPIC_API_KEY neu co.
for %%d in (input output logs auth work) do if not exist "%%d" mkdir "%%d"

echo.
echo Xong. Chay run_dashboard.bat de mo giao dien.
pause
exit /b 0

:fail
echo.
echo CAI DAT LOI - xem thong bao o tren.
pause
exit /b 1
