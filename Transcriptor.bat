@echo off
REM Lanceur de secours (le raccourci du bureau fait la meme chose sans console).
set "VENV=%LOCALAPPDATA%\Transcriptor\venv"
if not exist "%VENV%\Scripts\pythonw.exe" (
    echo Transcriptor n'est pas installe. Lance d'abord Installer.bat.
    pause & exit /b 1
)
start "" "%VENV%\Scripts\pythonw.exe" "%~dp0transcriptor.py" %1
