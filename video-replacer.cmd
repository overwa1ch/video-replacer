@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "REPO_ROOT=%~dp0"
if "%REPO_ROOT:~-1%"=="\" set "REPO_ROOT=%REPO_ROOT:~0,-1%"
set "PYTHON_BINARY=%REPO_ROOT%\.venv\Scripts\python.exe"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

if not exist "%PYTHON_BINARY%" (
  echo ERROR: repository runtime is missing; ask the repository Agent to install it. 1>&2
  exit /b 2
)

"%PYTHON_BINARY%" "%REPO_ROOT%\tools\windows_launcher.py" %*
exit /b %ERRORLEVEL%
