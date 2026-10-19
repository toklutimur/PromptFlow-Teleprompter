@echo off
cd /d "%~dp0"
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8765" ^| findstr "LISTENING"') do taskkill /PID %%p /F >nul 2>&1
start "PromptFlow alignment server" /min .venv312\Scripts\python.exe align_server.py
start "PromptFlow Studio" http://127.0.0.1:8765
