@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Local AI Setup - Ollama and Qwen

set "OLLAMA_EXE="
for %%O in (
  "%LOCALAPPDATA%\Programs\Ollama\ollama.exe"
  "%USERPROFILE%\AppData\Local\Programs\Ollama\ollama.exe"
) do (
  if not defined OLLAMA_EXE if exist "%%~O" set "OLLAMA_EXE=%%~O"
)
if not defined OLLAMA_EXE (
  for /f "delims=" %%O in ('where ollama.exe 2^>nul') do if not defined OLLAMA_EXE set "OLLAMA_EXE=%%O"
)

if not defined OLLAMA_EXE goto OLLAMA_NOT_FOUND

echo Ollama found:
echo %OLLAMA_EXE%
echo.
echo Installing the local Korean-capable model qwen3.5:4b.
echo The model download is approximately 3.4 GB and can take several minutes.
"%OLLAMA_EXE%" pull qwen3.5:4b
if errorlevel 1 goto MODEL_FAILED

echo.
echo Local AI setup completed.
echo Start the regulatory app with run_app.bat.
pause
exit /b 0

:OLLAMA_NOT_FOUND
echo.
echo Ollama is not installed.
echo Opening the official Windows download page.
start "" "https://ollama.com/download/windows"
echo Install Ollama, then run setup_local_ai.bat again.
pause
exit /b 1

:MODEL_FAILED
echo.
echo The model could not be downloaded.
echo Check the internet connection and confirm that Ollama is running.
pause
exit /b 1

