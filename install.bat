@echo off
rem Lingqiao installer for Windows: find Python 3.13+ and run app\windows_setup.py.
rem Messages from the setup script are in Chinese; this file stays ASCII so cmd.exe reads it correctly.
setlocal
cd /d "%~dp0"
set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 13) else 1)" >nul 2>&1 && set "PY=py -3"
if not defined PY (
  python -c "import sys; sys.exit(0 if sys.version_info >= (3, 13) else 1)" >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo.
  echo Lingqiao needs Python 3.13 or newer, but it was not found.
  echo Opening https://www.python.org/downloads/windows/ - install it, tick "Add python.exe to PATH",
  echo then double-click install.bat again.
  start "" "https://www.python.org/downloads/windows/"
  echo.
  pause
  exit /b 1
)
%PY% -X utf8 app\windows_setup.py %*
set "CODE=%ERRORLEVEL%"
echo.
pause
exit /b %CODE%
