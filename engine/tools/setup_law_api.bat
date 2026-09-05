@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Law API Setup

if not exist ".env" (
  if exist ".env.example" (
    copy /y ".env.example" ".env" >nul
  ) else (
    > ".env" echo LAW_OC=
  )
)

echo The .env configuration file will open in Notepad.
echo Enter the value issued by the National Law Information shared-use service after LAW_OC=.
echo Example: LAW_OC=your_issued_value
echo Save the file, close Notepad, and restart the application.
echo.
notepad ".env"
endlocal
