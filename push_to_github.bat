@echo off
cd /d "%~dp0"
echo === JobRadar GitHub Push ===
echo.

:: Remove partial .git if exists
if exist ".git" (
    echo Removing old .git folder...
    rmdir /s /q ".git"
)

:: Init
git init
git config user.name "Ranjith200228"
git config user.email "ranjithmaddirala24@gmail.com"

:: Stage all (gitignore excludes db, pyc, secrets)
git add .

:: Commit
git commit -m "Initial commit: JobRadar - AI-powered job search and resume tailoring app"

:: Remote + push
git remote add origin https://github.com/Ranjith200228/JobRadar.git
git branch -M main
git push -u origin main

echo.
echo === Done! Visit https://github.com/Ranjith200228/JobRadar ===
pause
