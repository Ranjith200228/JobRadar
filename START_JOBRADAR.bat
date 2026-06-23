@echo off
cd /d "%~dp0"
echo === JobRadar Startup Log === > startup.log
echo %date% %time% >> startup.log

echo Checking Python... >> startup.log
python --version >> startup.log 2>&1
if errorlevel 1 (
    echo ERROR: python not found, trying py... >> startup.log
    py --version >> startup.log 2>&1
    if errorlevel 1 (
        echo ERROR: Python not found in PATH. >> startup.log
        echo Python is not installed or not in PATH. Please install from python.org >> startup.log
        type startup.log
        pause
        exit /b 1
    )
    set PYTHON=py
) else (
    set PYTHON=python
)

echo Python OK. Installing packages... >> startup.log
%PYTHON% -m pip install -r requirements.txt >> startup.log 2>&1
echo Pip done. >> startup.log

echo Starting Flask... >> startup.log
echo.
echo ============================================
echo  JobRadar starting at http://localhost:5000
echo ============================================
echo.
%PYTHON% app.py >> startup.log 2>&1
echo Flask exited with code %errorlevel% >> startup.log
type startup.log
pause
