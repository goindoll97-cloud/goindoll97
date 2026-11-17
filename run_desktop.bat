@echo off
setlocal
cd /d "%~dp0"
title Chemical Regulatory Desktop - Developer Run

echo Run this after installing: python -m pip install -r requirements-desktop.txt
python desktop_app.py
if errorlevel 1 (
    echo.
    echo Program exited with an error.
    pause
)
