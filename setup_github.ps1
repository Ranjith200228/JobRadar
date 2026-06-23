# JobRadar - GitHub Setup Script
# Run this AFTER creating a new repo named "JobRadar" on GitHub
# Usage: Right-click this file > Run with PowerShell

$ErrorActionPreference = "Stop"
$repoPath = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $repoPath

Write-Host "`n=== JobRadar GitHub Setup ===" -ForegroundColor Cyan

# 1. Clean up any partial .git from previous attempts
if (Test-Path ".git") {
    Write-Host "Removing partial .git folder..." -ForegroundColor Yellow
    Remove-Item -Recurse -Force ".git"
}

# 2. Initialize fresh repo
Write-Host "Initializing git repository..." -ForegroundColor Green
git init
git config user.name "Ranjith200228"
git config user.email "ranjithmaddirala24@gmail.com"

# 3. Stage all files (.gitignore will exclude db, pyc, secrets)
Write-Host "Staging files..." -ForegroundColor Green
git add .

Write-Host "`nFiles to be committed:" -ForegroundColor Cyan
git status --short

# 4. Initial commit
git commit -m "Initial commit: JobRadar - AI-powered job search and resume tailoring app

- Flask backend with Apify job scraping (LinkedIn, Indeed, Glassdoor, ZipRecruiter, Wellfound, Dice)
- Claude AI resume tailoring, cover letter, cold email and LinkedIn outreach generation
- ReportLab PDF generation with pixel-perfect resume layout
- SQLite job tracking with ATS match scoring
- React 18 frontend with dark UI"

# 5. Set remote and push
Write-Host "`nConnecting to GitHub..." -ForegroundColor Green
git remote add origin https://github.com/Ranjith200228/JobRadar.git
git branch -M main
git push -u origin main

Write-Host "`n=== Done! JobRadar is live at: ===" -ForegroundColor Cyan
Write-Host "https://github.com/Ranjith200228/JobRadar" -ForegroundColor Green
