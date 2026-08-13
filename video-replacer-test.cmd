@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "REPO_ROOT=%~dp0"
if "%REPO_ROOT:~-1%"=="\" set "REPO_ROOT=%REPO_ROOT:~0,-1%"
set "PYTHON_BINARY=%REPO_ROOT%\.venv\Scripts\python.exe"
if defined VIDEO_REPLACER_NODE (
  set "NODE_BINARY=%VIDEO_REPLACER_NODE%"
) else (
  set "NODE_BINARY=node.exe"
)
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

if not exist "%PYTHON_BINARY%" (
  echo ERROR: repository runtime is missing; ask the repository Agent to install it. 1>&2
  exit /b 2
)

pushd "%REPO_ROOT%" || exit /b 2
"%PYTHON_BINARY%" -m unittest discover -s validation -p "test_*.py"
if errorlevel 1 goto failed
"%PYTHON_BINARY%" -m unittest discover -s tools -p "test_*.py"
if errorlevel 1 goto failed
"%NODE_BINARY%" --test tools\test_video_batch_orchestrator.mjs
if errorlevel 1 goto failed
"%NODE_BINARY%" --check tools\video_batch_orchestrator.mjs
if errorlevel 1 goto failed
"%PYTHON_BINARY%" -m py_compile tools\bootstrap.py tools\doctor.py tools\install_dreamina.py tools\setup.py tools\state_paths.py tools\codex_node_home.py tools\codex_artifact.py tools\codex_wire_attestation.py tools\windows_launcher.py tools\release_audit.py tools\backend_profiles.py tools\upload_preparation.py tools\video_batch_loop.py tools\dreamina_video.py tools\ark_video.py tools\prepare_reference_video.py tools\privacy\face_mosaic.py
if errorlevel 1 goto failed
"%PYTHON_BINARY%" tools\release_audit.py
if errorlevel 1 goto failed
popd
exit /b 0

:failed
set "RESULT=%ERRORLEVEL%"
popd
exit /b %RESULT%
