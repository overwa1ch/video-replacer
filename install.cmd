@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "REPO_ROOT=%~dp0"
if "%REPO_ROOT:~-1%"=="\" set "REPO_ROOT=%REPO_ROOT:~0,-1%"
set "REPOSITORY_PYTHON=%REPO_ROOT%\.venv\Scripts\python.exe"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

if defined VIDEO_REPLACER_BOOTSTRAP_PYTHON goto configured_python
if exist "%REPOSITORY_PYTHON%" goto repository_python

where py.exe >nul 2>nul
if errorlevel 1 goto python312
py.exe -3.12 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>nul
if errorlevel 1 goto python312
py.exe -3.12 "%REPO_ROOT%\tools\bootstrap.py" %*
exit /b %ERRORLEVEL%

:python312
where python3.12.exe >nul 2>nul
if errorlevel 1 goto python_default
python3.12.exe -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>nul
if errorlevel 1 goto python_default
python3.12.exe "%REPO_ROOT%\tools\bootstrap.py" %*
exit /b %ERRORLEVEL%

:python_default
where python.exe >nul 2>nul
if errorlevel 1 goto no_python
python.exe -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>nul
if errorlevel 1 goto no_python
python.exe "%REPO_ROOT%\tools\bootstrap.py" %*
exit /b %ERRORLEVEL%

:no_python
echo ERROR: Python 3.12 or newer is required. 1>&2
exit /b 1

:repository_python
"%REPOSITORY_PYTHON%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>nul
if errorlevel 1 goto python312
"%REPOSITORY_PYTHON%" "%REPO_ROOT%\tools\bootstrap.py" %*
exit /b %ERRORLEVEL%

:configured_python
"%VIDEO_REPLACER_BOOTSTRAP_PYTHON%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>nul
if errorlevel 1 goto configured_python_invalid
"%VIDEO_REPLACER_BOOTSTRAP_PYTHON%" "%REPO_ROOT%\tools\bootstrap.py" %*
exit /b %ERRORLEVEL%

:configured_python_invalid
echo ERROR: VIDEO_REPLACER_BOOTSTRAP_PYTHON is not Python 3.12 or newer. 1>&2
exit /b 1
