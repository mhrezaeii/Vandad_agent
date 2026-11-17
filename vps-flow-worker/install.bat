@echo off
rem Nesta Windows 11 VPS — Flow worker setup
rem Run from the vps-flow-worker folder (double-click or cmd).

echo == [1/3] python check ==
where python >nul 2>nul
if errorlevel 1 (
    echo Python not found. Install from https://www.python.org/downloads/
    echo IMPORTANT: check "Add python.exe to PATH" during install, then rerun this file.
    pause
    exit /b 1
)
python --version

echo == [2/3] dependencies ==
python -m pip install --upgrade playwright httpx

echo == [3/3] chromium ==
python -m playwright install chromium

echo.
echo === Setup done ===
echo Next steps:
echo   1. python flow_worker.py login        (a browser opens - log into labs.google/flow once)
echo   2. python flow_worker.py status
echo   3. python flow_worker.py generate --prompt-file prompts-vandiyar.txt --out clips
echo.
pause
