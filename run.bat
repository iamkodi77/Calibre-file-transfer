@echo off
setlocal
rem Start Leafline from the folder containing this script.
pushd "%~dp0" || goto directory_error

if exist ".venv\Scripts\python.exe" goto check_venv
if exist ".venv" goto invalid_venv

rem Prefer the Windows Python launcher, then try Python on PATH.
where py >nul 2>&1
if errorlevel 1 goto try_python
py -3 -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if errorlevel 1 goto try_python
set "PYTHON_CMD=py -3"
goto create_venv

:try_python
where python >nul 2>&1
if errorlevel 1 goto python_missing
python -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if errorlevel 1 goto python_missing
set "PYTHON_CMD=python"

:create_venv
echo Creating the Leafline virtual environment...
%PYTHON_CMD% -m venv ".venv"
if errorlevel 1 goto venv_error

:check_venv
".venv\Scripts\python.exe" -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if errorlevel 1 goto invalid_venv

echo Installing/checking Leafline requirements...
".venv\Scripts\python.exe" -m pip install -r "requirements.txt"
if errorlevel 1 goto install_error

echo Starting Leafline. Press Ctrl+C to stop.
".venv\Scripts\python.exe" "app.py" %*
set "APP_EXIT=%ERRORLEVEL%"
popd
exit /b %APP_EXIT%

:python_missing
echo Error: Python 3.10 or newer is required. Install Python and try again.
echo On Windows, install Python from python.org and enable "Add Python to PATH".
goto error

:invalid_venv
echo Error: .venv exists but does not contain a usable Windows Python 3.10 or newer.
echo Remove or rename .venv, then run this script again.
goto error

:venv_error
echo Error: could not create .venv. Check your Python installation and try again.
echo If a partial .venv was created, remove or rename it before retrying.
goto error

:install_error
echo Error: requirements could not be installed. Check your internet connection
echo and Python/pip installation, then try again.
goto error

:error
echo.
echo Press any key to close this window.
pause >nul
popd
exit /b 1

:directory_error
echo Error: could not open the folder containing run.bat.
echo Press any key to close this window.
pause >nul
exit /b 1
