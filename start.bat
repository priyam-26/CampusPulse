@echo off
REM College Event System - Startup Script for Windows

echo.
echo 🎓 College Event System Startup
echo ================================
echo.

REM Check if .env exists
if not exist ".env" (
    echo ⚠️  .env file not found!
    echo Creating .env from .env.example...
    copy .env.example .env
    echo ✅ .env created. Please edit it if needed.
    echo.
)

REM Check Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo ❌ Python not found. Please install Python 3.8+
    echo Download from: https://www.python.org/downloads/
    pause
    exit /b 1
)

echo ✅ Python found
echo.

REM Check if venv exists
if not exist "venv\" (
    echo 📦 Creating virtual environment...
    python -m venv venv
    echo ✅ Virtual environment created
    echo.
)

REM Activate venv
echo 🔄 Activating virtual environment...
call venv\Scripts\activate.bat
echo ✅ Virtual environment activated
echo.

REM Install requirements
echo 📥 Installing dependencies...
python -m pip install -q -r requirements.txt
if %errorlevel% neq 0 (
    echo ❌ Failed to install dependencies
    pause
    exit /b 1
)
echo ✅ Dependencies installed
echo.

REM Run app
echo 🚀 Starting application...
echo 📍 Visit: http://localhost:5000
echo 👤 Admin Login: admin / priyam123
echo.
echo Press Ctrl+C to stop the server
echo.

python app.py

pause
