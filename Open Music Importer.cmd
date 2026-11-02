@echo off
setlocal
if not exist "%~dp0.venv\Scripts\pythonw.exe" (
    echo Python environment not found. See README.md for setup.
    pause
    exit /b 1
)
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0music_import_ui.py" %*
