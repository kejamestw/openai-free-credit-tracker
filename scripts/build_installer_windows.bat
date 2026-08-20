@echo off
setlocal EnableExtensions
cd /d "%~dp0\.."

set "PYTHON_BIN=python"
if exist ".venv\Scripts\python.exe" set "PYTHON_BIN=.venv\Scripts\python.exe"

for /f "usebackq delims=" %%V in (`"%PYTHON_BIN%" -c "from quota_monitor import __version__; print(__version__)"`) do set "APP_VERSION=%%V"
if not defined APP_VERSION exit /b 1

if not exist "dist\OpenAI-Free-Credit-Tracker.exe" call scripts\build_windows.bat
if errorlevel 1 exit /b %errorlevel%

set "ISCC_BIN=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC_BIN%" set "ISCC_BIN=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC_BIN%" (
  echo Inno Setup 6 compiler was not found. 1>&2
  exit /b 1
)

"%ISCC_BIN%" /DAppVersion=%APP_VERSION% /DSourceExe="%CD%\dist\OpenAI-Free-Credit-Tracker.exe" /DOutputDirectory="%CD%\dist" installer\windows\OpenAI-Free-Credit-Tracker.iss
if errorlevel 1 exit /b %errorlevel%

set "INSTALLER=dist\OpenAI-Free-Credit-Tracker-%APP_VERSION%-windows-x86_64-setup.exe"
if not exist "%INSTALLER%" exit /b 1
"%INSTALLER%" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /DIR="%CD%\build\installer-smoke"
if errorlevel 1 exit /b %errorlevel%
"%CD%\build\installer-smoke\OpenAI-Free-Credit-Tracker.exe" --version
if errorlevel 1 exit /b %errorlevel%
"%CD%\build\installer-smoke\unins000.exe" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
if errorlevel 1 exit /b %errorlevel%

echo Windows installer %APP_VERSION% build and smoke test passed
exit /b 0
