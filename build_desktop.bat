@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Build Chemical Regulatory Desktop for Windows

echo [1/4] Checking Python...
python --version
if errorlevel 1 goto FAILED

echo [2/4] Installing desktop build dependencies...
python -m pip install -r requirements-desktop.txt
if errorlevel 1 goto FAILED

echo [3/4] Checking Python syntax...
python -m py_compile desktop_app.py desktop_backend.py desktop_smoke_test.py
if errorlevel 1 goto FAILED

echo [4/4] Building the Windows desktop program...
python -m PyInstaller --noconfirm --clean desktop.spec
if errorlevel 1 goto FAILED

echo.
echo Build complete:
echo   dist\ChemicalRegulatoryDesktop\ChemicalRegulatoryDesktop.exe
echo.
echo Copy the ENTIRE dist\ChemicalRegulatoryDesktop folder to the presentation PC.
echo Keep API credentials in a separate .env file next to the EXE, if needed.
echo Do not upload .env to GitHub.
pause
exit /b 0

:FAILED
echo.
echo ERROR: Build did not complete. Read the log above.
pause
exit /b 1
