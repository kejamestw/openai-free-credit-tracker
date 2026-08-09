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

echo Python 3.10 or newer was not found. Install Python or create .venv before building.
exit /b 1

:python_found
"%PYTHON_EXE%" %PYTHON_ARGS% -m pip install -r requirements-dev.txt
if errorlevel 1 exit /b 1

"%PYTHON_EXE%" %PYTHON_ARGS% -m pip install -e .
if errorlevel 1 exit /b 1

"%PYTHON_EXE%" %PYTHON_ARGS% -m PyInstaller --noconfirm --clean --onefile --name OpenAI-Free-Credit-Tracker --paths src --add-data "web;web" --add-data "data;data" scripts\windows_entry.py
if errorlevel 1 exit /b 1

if not exist "dist\OpenAI-Free-Credit-Tracker.exe" (
  echo Build failed: expected executable was not created.
  exit /b 1
)

"dist\OpenAI-Free-Credit-Tracker.exe" --smoke-test
if errorlevel 1 exit /b 1

"dist\OpenAI-Free-Credit-Tracker.exe" --version
if errorlevel 1 exit /b 1

echo Build and smoke test completed successfully.
exit /b 0
