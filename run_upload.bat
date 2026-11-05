@echo off
rem run_upload.bat <batch> [--yes] [--retry-errors] [--no-publish] [--dry-run] [--limit N]
setlocal
cd /d "%~dp0"
call "%~dp0_python.bat" || exit /b 1
if "%~1"=="" (
  "%PY%" upload_batch.py
) else (
  "%PY%" upload_batch.py --batch %*
)
pause
