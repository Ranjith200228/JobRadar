@echo off
echo ================================
echo  Python Check for JobRadar
echo ================================
echo.

python --version
if errorlevel 1 (
    echo.
    echo PROBLEM: "python" command not found.
    echo Try running:  py --version
    echo.
    py --version
) else (
    echo.
    echo Python found! Testing port 5000...
    echo Open http://localhost:5000 in your browser.
    echo Press Ctrl+C to stop this test.
    echo.
    python -c "import http.server, socketserver; print('Server running on port 5000'); socketserver.TCPServer(('',5000), http.server.SimpleHTTPRequestHandler).serve_forever()"
)

echo.
pause
