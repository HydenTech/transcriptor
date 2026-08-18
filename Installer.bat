@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
title Transcriptor — installation

set "RACINE=%~dp0"
set "VENV=%LOCALAPPDATA%\Transcriptor\venv"

echo.
echo   ============================================================
echo    Transcriptor — installation
echo    Une seule fois. Environ 10 minutes, 4 Go.
echo   ============================================================
echo.

REM --- 1. Python ---------------------------------------------------
echo   [1/5] Python
where py >nul 2>&1 || where python >nul 2>&1
if errorlevel 1 (
    echo         Installation de Python...
    winget install -e --id Python.Python.3.12 --silent --accept-source-agreements --accept-package-agreements
    if errorlevel 1 (
        echo.
        echo   Python n'a pas pu s'installer automatiquement.
        echo   Telecharge-le sur https://www.python.org/downloads/
        echo   en cochant "Add python.exe to PATH", puis relance ce fichier.
        echo.
        pause & exit /b 1
    )
    set "PATH=%LOCALAPPDATA%\Programs\Python\Python312;%LOCALAPPDATA%\Programs\Python\Python312\Scripts;%PATH%"
)

set "PY=py"
where py >nul 2>&1 || set "PY=python"

REM --- 2. Environnement ---------------------------------------------
echo   [2/5] Environnement Python
%PY% -m venv "%VENV%" || (
    echo   Creation de l'environnement impossible.
    pause & exit /b 1
)
call "%VENV%\Scripts\activate.bat"
python -m pip install --quiet --upgrade pip

REM --- 3. Bibliotheques ---------------------------------------------
echo   [3/5] Moteur de transcription et interface
python -m pip install --quiet ^
    "pywebview>=5.1" "faster-whisper>=1.1" "markdown>=3.5" "playwright>=1.44" ^
    "nvidia-cublas-cu12" "nvidia-cudnn-cu12>=9,<10"
if errorlevel 1 (
    echo   Installation des bibliotheques echouee. Verifie ta connexion.
    pause & exit /b 1
)

echo   [4/5] Chromium ^(mise en page PDF^)
python -m playwright install chromium >nul 2>&1 || (
    echo         Chromium indisponible : les PDF seront ignores,
    echo         le Markdown restera produit.
)

REM --- 4. Claude Code ------------------------------------------------
echo   [5/5] Claude Code
where claude >nul 2>&1
if errorlevel 1 (
    where npm >nul 2>&1
    if errorlevel 1 (
        winget install -e --id OpenJS.NodeJS.LTS --silent --accept-source-agreements --accept-package-agreements
        set "PATH=%ProgramFiles%\nodejs;%APPDATA%\npm;%PATH%"
    )
    call npm install -g @anthropic-ai/claude-code
)

REM --- 5. Raccourci bureau -------------------------------------------
powershell -NoProfile -Command ^
  "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\Transcriptor.lnk');" ^
  "$s.TargetPath='%VENV%\Scripts\pythonw.exe';" ^
  "$s.Arguments='\"%RACINE%transcriptor.py\"';" ^
  "$s.WorkingDirectory='%RACINE%';" ^
  "$s.Description='Transcrire et resumer un cours';" ^
  "$s.IconLocation='%RACINE%static\transcriptor.ico';" ^
  "$s.Save()" >nul 2>&1

echo.
echo   ------------------------------------------------------------
echo    Derniere etape : connecter Claude Code a ton abonnement.
echo    Une session va s'ouvrir. Suis la procedure, puis Ctrl+C.
echo   ------------------------------------------------------------
echo.
pause

call claude

echo.
echo   ============================================================
echo    Termine. Un raccourci "Transcriptor" est sur ton bureau.
echo    Tu peux aussi y deposer directement un fichier audio.
echo   ============================================================
echo.
pause
