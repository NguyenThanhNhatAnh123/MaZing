@echo off
rem Picks the Python of this folder: portable python\ first, then .venv\
if exist "%~dp0python\python.exe" (
  set "PY=%~dp0python\python.exe"
) else if exist "%~dp0.venv\Scripts\python.exe" (
  set "PY=%~dp0.venv\Scripts\python.exe"
) else (
  echo Chua cai dat: chay setup.bat truoc.
  pause
  exit /b 1
)
exit /b 0
