@echo off
chcp 65001 >nul
REM Используем общее окружение VKR; запасной вариант — активный Python.
set "DIAMOND_APP_PYTHON=%~dp0..\venv\Scripts\python.exe"
if not exist "%DIAMOND_APP_PYTHON%" set "DIAMOND_APP_PYTHON=python"
cd /d "%~dp0"
"%DIAMOND_APP_PYTHON%" "%~dp0main.py"
if errorlevel 1 pause
