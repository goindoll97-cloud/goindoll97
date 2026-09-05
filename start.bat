@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Company Chemical Regulatory Screening - Setup and Run

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

if defined CONDA_EXE goto USE_CONDA
goto USE_STANDARD_PYTHON

:USE_CONDA
echo [1/4] Miniconda/Anaconda found:
echo       %CONDA_EXE%
echo.
echo [2/4] Checking the dedicated environment: %APP_ENV%
"%CONDA_EXE%" run -n "%APP_ENV%" python --version >nul 2>&1
if errorlevel 1 (
  echo       Creating a new environment. This can take several minutes.
  "%CONDA_EXE%" create -y -n "%APP_ENV%" python=3.12 pip
  if errorlevel 1 goto INSTALL_FAILED
) else (
  echo       Existing environment found.
)

echo.
echo [3/4] Installing required packages...
"%CONDA_EXE%" run -n "%APP_ENV%" python -m pip install --upgrade pip
if errorlevel 1 goto INSTALL_FAILED
"%CONDA_EXE%" run -n "%APP_ENV%" python -m pip install -r requirements.txt
if errorlevel 1 goto INSTALL_FAILED

echo.
echo [4/4] Starting the company regulatory screening web app...
"%CONDA_EXE%" run --no-capture-output -n "%APP_ENV%" python -m streamlit run app.py
if errorlevel 1 goto RUN_FAILED
goto END

:USE_STANDARD_PYTHON
echo Miniconda/Anaconda was not found. Trying the standard Python launcher.
where py.exe >nul 2>&1
if not errorlevel 1 (
  set "PY_CMD=py -3"
) else (
  where python.exe >nul 2>&1
  if errorlevel 1 goto PYTHON_NOT_FOUND
  set "PY_CMD=python"
)

%PY_CMD% -m pip --version >nul 2>&1
if errorlevel 1 (
  echo pip is missing. Trying Python's built-in ensurepip...
  %PY_CMD% -m ensurepip --upgrade
  if errorlevel 1 goto PIP_NOT_FOUND
)
%PY_CMD% -m pip install --upgrade pip
if errorlevel 1 goto INSTALL_FAILED
%PY_CMD% -m pip install -r requirements.txt
if errorlevel 1 goto INSTALL_FAILED
%PY_CMD% -m streamlit run app.py
if errorlevel 1 goto RUN_FAILED
goto END

:PYTHON_NOT_FOUND
echo.
echo ERROR: Python and Miniconda/Anaconda were not found.
echo Install Miniconda or Python 3.11/3.12, then run this file again.
goto FAILED

:PIP_NOT_FOUND
echo.
echo ERROR: pip could not be installed in the selected Python environment.
echo If Miniconda is installed, check that Scripts\conda.exe exists.
goto FAILED

:INSTALL_FAILED
echo.
echo ERROR: Package installation failed.
echo Check the internet connection and the error message shown above.
goto FAILED

:RUN_FAILED
echo.
echo ERROR: The dashboard could not start.
goto FAILED

:FAILED
echo.
pause
exit /b 1

:END
endlocal
