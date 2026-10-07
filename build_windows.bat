@echo off
REM ====================================================================
REM  Builds DBSearch.exe (GUI) and dbsearch.exe (command line).
REM  Both are standalone - no Python needed on the machine that runs them.
REM  Run this once on a Windows PC that has Python 3.8+ installed.
REM ====================================================================
setlocal
cd /d "%~dp0"

echo [1/4] Checking Python...
python --version || (echo ERROR: Python not found on PATH. Install from python.org and tick "Add Python to PATH". & pause & exit /b 1)

echo [2/4] Installing PyInstaller...
python -m pip install --upgrade pyinstaller || (echo ERROR: pip install failed. & pause & exit /b 1)

echo [3/4] Building the GUI (DBSearch.exe)...
REM --windowed  = no console window behind the GUI
REM --add-data  = bundle the Roboto fonts (Windows uses ';' as the separator)
python -m PyInstaller --onefile --windowed --name DBSearch ^
    --add-data "fonts;fonts" ^
    dbsearch_gui.py || (echo ERROR: GUI build failed. & pause & exit /b 1)

echo [4/4] Building the command-line tool (dbsearch.exe)...
python -m PyInstaller --onefile --console --name dbsearch ^
    dbsearch.py || (echo ERROR: CLI build failed. & pause & exit /b 1)

echo.
echo ============================================================
echo  DONE.
echo    dist\DBSearch.exe   - double-click for the dark-mode GUI
echo    dist\dbsearch.exe   - command line version
echo.
echo  Both are self-contained; copy them anywhere.
echo ============================================================
pause
