@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Company Chemical Regulatory Screening

set "APP_ENV=broad_salt_app"
set "CONDA_EXE="

for %%C in (
  "%USERPROFILE%\miniconda3\Scripts\conda.exe"
  "%USERPROFILE%\Miniconda3\Scripts\conda.exe"
  "%USERPROFILE%\anaconda3\Scripts\conda.exe"
  "%USERPROFILE%\Anaconda3\Scripts\conda.exe"
  "%LOCALAPPDATA%\miniconda3\Scripts\conda.exe"
  "%LOCALAPPDATA%\anaconda3\Scripts\conda.exe"
  "%ProgramData%\miniconda3\Scripts\conda.exe"
  "%ProgramData%\anaconda3\Scripts\conda.exe"
) do (
  if not defined CONDA_EXE if exist "%%~C" set "CONDA_EXE=%%~C"
)

if not defined CONDA_EXE (
  for /f "delims=" %%C in ('where conda.exe 2^>nul') do (
    if not defined CONDA_EXE set "CONDA_EXE=%%C"
  )
)

if defined CONDA_EXE (
  "%CONDA_EXE%" run -n "%APP_ENV%" python --version >nul 2>&1
  if errorlevel 1 (
    echo The application environment is not installed yet.
    echo Starting the installer...
    call install_and_run.bat
    if errorlevel 1 exit /b 1
    exit /b 0
  )
  "%CONDA_EXE%" run --no-capture-output -n "%APP_ENV%" python -m streamlit run app.py
  if errorlevel 1 (
    pause
    exit /b 1
  )
  exit /b 0
)

where py.exe >nul 2>&1
if not errorlevel 1 (
  py -3 -m streamlit run app.py
) else (
  python -m streamlit run app.py
)
if errorlevel 1 (
  echo.
  echo The required environment is missing. Run install_and_run.bat first.
  pause
)
endlocal
