@echo off
cd /d "%~dp0"
start "" venv\Scripts\python.exe app.py
timeout /t 2 /nobreak >nul
start "" http://127.0.0.1:5000
