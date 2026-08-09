@echo off
setlocal
cd /d "%~dp0\.."
if errorlevel 1 exit /b 1

set "PYTHON_EXE=%CD%\.venv\Scripts\python.exe"
set "PYTHON_ARGS="
if exist "%PYTHON_EXE%" goto python_found

set "PYTHON_EXE=py"
set "PYTHON_ARGS=-3"
"%PYTHON_EXE%" %PYTHON_ARGS% --version >nul 2>&1
if not errorlevel 1 goto python_found

set "PYTHON_EXE=python"
set "PYTHON_ARGS="
"%PYTHON_EXE%" --version >nul 2>&1
if not errorlevel 1 goto python_found

echo Python 3.10 or newer was not found. Install Python, create .venv, or use the portable EXE.
if "%~1"=="" pause
exit /b 1

:python_found
set PYTHONPATH=%CD%\src
"%PYTHON_EXE%" %PYTHON_ARGS% -m quota_monitor %*
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" echo OpenAI Free Credit Tracker exited with code %EXIT_CODE%.
if "%~1"=="" pause
exit /b %EXIT_CODE%
