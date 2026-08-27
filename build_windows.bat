@echo off
setlocal
cd /d "%~dp0"

echo ===============================================
echo   Cake Powder / Faithful Few
echo   OSRS World Tracker - Windows Build
echo ===============================================
echo.

where py >nul 2>nul
if %errorlevel%==0 goto PYFOUND
where python >nul 2>nul
if %errorlevel%==0 goto PYFOUND

echo Python was not found.
echo.
echo You do NOT need Python to run the finished EXE.
echo This script is only for building the EXE locally.
echo.
echo Easiest option: use the GitHub Actions build described in README.md.
echo.
pause
exit /b 1

:PYFOUND
py -3 -m pip install --upgrade pyinstaller
if errorlevel 1 goto FAIL

if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist "Cake Powder OSRS World Tracker.spec" del /q "Cake Powder OSRS World Tracker.spec"

py -3 -m PyInstaller --noconfirm --clean --onefile --windowed --name "Cake Powder OSRS World Tracker" world_tracker.py
if errorlevel 1 goto FAIL

echo.
echo ===============================================
echo BUILD COMPLETE
echo ===============================================
echo.
echo Your standalone application is:
echo dist\Cake Powder OSRS World Tracker.exe
echo.
echo Copy that EXE anywhere you like. Python is NOT required to run it.
echo.
pause
exit /b 0

:FAIL
echo.
echo BUILD FAILED.
echo Check the error above.
pause
exit /b 1
