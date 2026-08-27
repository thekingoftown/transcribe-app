@echo off
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python is not installed. Install it from https://www.python.org/downloads/
    echo IMPORTANT: check "Add Python to PATH" during install.
    pause
    exit /b 1
)

if not exist venv (
    echo Creating virtual environment...
    python -m venv venv
)

call venv\Scripts\activate.bat

pip show faster-whisper >nul 2>nul
if errorlevel 1 (
    echo Installing dependencies - this only happens once...
    pip install -r requirements.txt
)

start "" http://localhost:5005
python app.py
pause
