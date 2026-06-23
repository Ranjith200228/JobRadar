Set-Location $PSScriptRoot

Write-Host "=== JobRadar Startup ===" -ForegroundColor Cyan

# Find Python
$python = $null
foreach ($cmd in @("python", "python3", "py")) {
    try {
        $ver = & $cmd --version 2>&1
        if ($LASTEXITCODE -eq 0) {
            Write-Host "Found: $cmd -> $ver" -ForegroundColor Green
            $python = $cmd
            break
        }
    } catch {}
}

if (-not $python) {
    Write-Host "ERROR: Python not found. Please install from python.org and check 'Add to PATH'." -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}

Write-Host "`nInstalling packages..." -ForegroundColor Yellow
& $python -m pip install -r requirements.txt

Write-Host "`nStarting JobRadar at http://localhost:5000 ..." -ForegroundColor Green
Write-Host "Press Ctrl+C to stop.`n" -ForegroundColor Gray

& $python app.py
Read-Host "Press Enter to exit"
