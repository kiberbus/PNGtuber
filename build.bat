@echo off
cd /d "%~dp0"

venv\Scripts\python.exe -m pip install --upgrade pyinstaller || exit /b 1
venv\Scripts\python.exe -m PyInstaller --noconfirm --onefile --windowed --name PNGtuber ^
    --distpath dist --workpath build --specpath build main.py || exit /b 1

copy /y *.png dist\ >nul
if exist expressions xcopy /e /i /y /q expressions dist\expressions >nul
if exist config.json copy /y config.json dist\ >nul

echo.
echo Done: dist\PNGtuber.exe
