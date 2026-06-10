@echo off
REM ─────────────────────────────────────────────────────────────────────────────
REM  PinPointX Startup Script (Windows)
REM  Usage: startup.bat
REM ─────────────────────────────────────────────────────────────────────────────

cd /d "%~dp0"

echo [PinPointX] Starting on Windows...
echo [PinPointX] NOTE: EMBA firmware analysis requires Linux.
echo [PinPointX] PCB analysis, hardware analysis, and all other features work normally.
echo.

docker compose version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Docker Desktop not found. Install Docker Desktop first.
    pause
    exit /b 1
)

echo [PinPointX] Pulling latest image...
docker compose pull
if %errorlevel% neq 0 (
    echo [WARNING] Image pull failed. Using cached image if available.
)

echo [PinPointX] Starting app container...
docker compose up