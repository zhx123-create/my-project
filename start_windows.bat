@echo off
setlocal
pushd "%~dp0"
where py >nul 2>nul
if errorlevel 1 goto fallback
py -3.13 -c "import sys" >nul 2>nul
if errorlevel 1 goto default_python
py -3.13 bootstrap_desktop.py
if errorlevel 1 goto failed
goto done
:default_python
py -3 bootstrap_desktop.py
if errorlevel 1 goto failed
goto done
:fallback
where python >nul 2>nul
if errorlevel 1 goto missing
python bootstrap_desktop.py
if errorlevel 1 goto failed
goto done
:missing
echo Install Python 3.10 or newer from python.org. Python 3.13 with Tcl/Tk and pip is recommended.
goto failed
:failed
echo.
echo Setup or launch failed. Read the message above. Your existing environment was not deleted.
pause
popd
exit /b 1
:done
popd
exit /b 0
