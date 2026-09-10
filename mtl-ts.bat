@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "MENU_DIR=%SCRIPT_DIR%ts"

if not exist "%MENU_DIR%\node_modules" (
    echo Installing TypeScript menu dependencies...
    pushd "%MENU_DIR%" >nul
    call npm install
    if errorlevel 1 (
        popd >nul
        echo Failed to install TypeScript menu dependencies.
        exit /b 1
    )
    popd >nul
)

pushd "%MENU_DIR%" >nul
call npm start
set "EXIT_CODE=%ERRORLEVEL%"
popd >nul

exit /b %EXIT_CODE%
