@echo off
setlocal
pushd "%~dp0"
if exist ".venv\Scripts\python.exe" goto dependencies
where py >nul 2>nul
if errorlevel 1 goto fallback
py -3 -m venv .venv
if errorlevel 1 goto failed
goto dependencies
:fallback
where python >nul 2>nul
if errorlevel 1 goto missing
python -m venv .venv
if errorlevel 1 goto failed
:dependencies
".venv\Scripts\python.exe" -c "import sys; assert sys.version_info >= (3,10)"
if errorlevel 1 goto missing
".venv\Scripts\python.exe" -c "from PIL import __version__; assert __version__ == '12.0.0'; import tkinter" >nul 2>nul
if not errorlevel 1 goto launch
".venv\Scripts\python.exe" -m pip install -r requirements-desktop.txt
if errorlevel 1 goto failed
:launch
".venv\Scripts\python.exe" desktop.py
if errorlevel 1 goto failed
popd
exit /b 0
:missing
echo Install Python 3.10 or newer from python.org, including Tcl/Tk and the Python launcher.
goto failed
:failed
echo Setup or launch failed. Read the message above. No security verification was disabled.
pause
popd
exit /b 1
