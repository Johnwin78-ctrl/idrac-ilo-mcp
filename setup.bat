@echo off
title bmc-mcp setup
cd /d "%~dp0"
where py >nul 2>&1
if %errorlevel%==0 (
    py -3 setup.py
) else (
    python setup.py
)
echo.
pause
