@echo off
REM Database cleanup utility launcher for Windows
REM Usage: cleanup_database.bat [command] [args]

echo Starting database cleanup utility...
echo.

python cleanup_database.py %*

if errorlevel 1 (
    echo.
    echo Error: Failed to run cleanup script
    echo Make sure you have Python installed and are in the correct directory
    pause
)
