@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

set "PYTHON_EXE="
if exist "%~dp0.runtime-python.txt" set /p "PYTHON_EXE="<"%~dp0.runtime-python.txt"
if not defined PYTHON_EXE if exist "%~dp0.venv\Scripts\python.exe" set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"

if not defined PYTHON_EXE goto runtime_missing
if not exist "%PYTHON_EXE%" goto runtime_missing

"%PYTHON_EXE%" --version
if errorlevel 1 goto runtime_broken

"%PYTHON_EXE%" main.py --no-wait
set "BRIDGE_EXIT=%ERRORLEVEL%"
if not "%BRIDGE_EXIT%"=="0" (
    echo.
    echo [ERROR] v3 failed. Exit code: %BRIDGE_EXIT%
    echo.
    pause
)
exit /b %BRIDGE_EXIT%

:runtime_missing
echo [ERROR] Runtime environment is not configured or no longer exists.
echo Run first.bat once, then start.bat will remember the selection.
echo.
pause
exit /b 2

:runtime_broken
echo.
echo [ERROR] The selected Python runtime cannot start.
echo Run first.bat again to select or repair the environment.
echo.
pause
exit /b 9009
