@echo off
setlocal
cd /d "%~dp0\.."
if errorlevel 1 exit /b 1

python -m pip install -r requirements-dev.txt
if errorlevel 1 exit /b 1

python -m pip install -e .
if errorlevel 1 exit /b 1

python -m PyInstaller --noconfirm --clean --onefile --name OpenAI-Free-Credit-Tracker --paths src --add-data "web;web" --add-data "data;data" src\quota_monitor\app.py
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
