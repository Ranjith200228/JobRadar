@echo off
cd /d "%~dp0"
title JobRadar - GitHub Push
echo.
echo ============================================
echo   JobRadar ^> GitHub Push Script
echo ============================================
echo.

:: Check git is available
git --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Git not found. Install Git from https://git-scm.com
    pause & exit /b 1
)

:: Remove broken .git if exists and reinit
if exist ".git" (
    echo Removing old .git folder...
    rmdir /s /q ".git"
)

echo [1/5] Initializing repository...
git init
git config user.name "Ranjith200228"
git config user.email "ranjithmaddirala24@gmail.com"

echo.
echo [2/5] Staging files...
git add .

echo.
echo [3/5] Creating commit...
git commit -m "Initial commit: JobRadar - AI-powered job search and resume tailoring app"

echo.
echo [4/5] Setting remote...
git remote add origin https://github.com/Ranjith200228/JobRadar.git
git branch -M main

echo.
echo [5/5] Pushing to GitHub...
echo (A browser window may open for GitHub login - complete it then return here)
git push -u origin main

echo.
if errorlevel 1 (
    echo PUSH FAILED - check the error above
) else (
    echo ============================================
    echo   SUCCESS! JobRadar is live at:
    echo   https://github.com/Ranjith200228/JobRadar
    echo ============================================
)
echo.
pause
