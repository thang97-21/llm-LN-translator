@echo off
REM ============================================
REM LLM Translator - Windows CLI Launcher
REM ============================================
REM Usage:
REM   mtl extract <epub>
REM   mtl translate <volume_id>
REM   mtl build <volume_id>
REM   mtl run <epub>
REM   mtl list
REM ============================================

setlocal enabledelayedexpansion

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

set "VENV_PYTHON=%SCRIPT_DIR%venv\Scripts\python.exe"
set "PYTHON_CMD="

if exist "%VENV_PYTHON%" (
    set "PYTHON_CMD=%VENV_PYTHON%"
    goto :found_python
)

python --version >nul 2>&1
if %ERRORLEVEL% equ 0 (
    set "PYTHON_CMD=python"
    goto :found_python
)

py --version >nul 2>&1
if %ERRORLEVEL% equ 0 (
    set "PYTHON_CMD=py"
    goto :found_python
)

echo.
echo ERROR: Python 3.10+ is required but not found.
echo Install from https://www.python.org/downloads/ and re-run.
echo.
pause
exit /b 1

:found_python

"%PYTHON_CMD%" -c "import anthropic, yaml, dotenv, lxml, bs4, PIL, tiktoken" >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo Installing required dependencies...
    "%PYTHON_CMD%" -m pip install -r "%SCRIPT_DIR%requirements.txt" -q
    "%PYTHON_CMD%" -c "import anthropic, yaml, dotenv, lxml, bs4, PIL, tiktoken" >nul 2>&1
    if %ERRORLEVEL% neq 0 (
        echo.
        echo ERROR: Failed to install dependencies.
        echo Please run: "%PYTHON_CMD%" -m pip install -r "%SCRIPT_DIR%requirements.txt"
        echo.
        pause
        exit /b 1
    )
    echo Dependencies installed.
)

"%PYTHON_CMD%" "%SCRIPT_DIR%scripts\mtl.py" %*

endlocal
