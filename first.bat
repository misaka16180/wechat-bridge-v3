@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

set "BOOTSTRAP_PYTHON="
set "BOOTSTRAP_ARGS="
if defined WECHAT_PYTHON set "BOOTSTRAP_PYTHON=%WECHAT_PYTHON%"
if not defined BOOTSTRAP_PYTHON (
    where python >nul 2>nul
    if not errorlevel 1 (
        python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
        if not errorlevel 1 set "BOOTSTRAP_PYTHON=python"
    )
)
if not defined BOOTSTRAP_PYTHON (
    where py >nul 2>nul
    if not errorlevel 1 (
        py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
        if not errorlevel 1 (
            set "BOOTSTRAP_PYTHON=py"
            set "BOOTSTRAP_ARGS=-3"
        )
    )
)
if not defined BOOTSTRAP_PYTHON goto python_missing

"%BOOTSTRAP_PYTHON%" %BOOTSTRAP_ARGS% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 goto python_broken

"%BOOTSTRAP_PYTHON%" %BOOTSTRAP_ARGS% --version
if errorlevel 1 (
    goto python_broken
)

"%BOOTSTRAP_PYTHON%" %BOOTSTRAP_ARGS% install.py --no-wait
set "SETUP_EXIT=%ERRORLEVEL%"
if not "%SETUP_EXIT%"=="0" (
    if "%SETUP_EXIT%"=="130" (
        echo.
        echo Environment setup cancelled. No new runtime was saved.
    ) else (
        echo.
        echo [ERROR] Environment setup failed. Exit code: %SETUP_EXIT%
    )
) else (
    echo.
    echo Environment setup completed.
)
echo.
pause
exit /b %SETUP_EXIT%

:python_missing
echo [ERROR] Python 3.10 or newer was not found. Environment setup did not start.
echo Install Python 3.10+ or set WECHAT_PYTHON to a valid python.exe path.
echo.
pause
exit /b 9009

:python_broken
echo.
echo [ERROR] The selected Python cannot start or is older than Python 3.10.
echo Check WECHAT_PYTHON or repair or upgrade the Python installation.
echo.
pause
exit /b 9009
