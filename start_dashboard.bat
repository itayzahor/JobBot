@echo off
cd /d "%~dp0"
powershell -NoProfile -Command "if (-not (Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue)) { Start-Process -FilePath 'venv\Scripts\python.exe' -ArgumentList 'app.py' -WorkingDirectory '%~dp0' }"
timeout /t 2 /nobreak >nul
start "" http://127.0.0.1:5000
