@echo off
REM Build the Windows standalone executable using PyInstaller.
REM Requires Python, pip, and PyInstaller installed.

pip install -r requirements.txt
pip install pyinstaller

REM Clean previous builds
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist LastZBot.spec del /q LastZBot.spec

python -m PyInstaller --noconfirm --onefile --name LastZBot --uac-admin ^
  --add-data "templates;templates" ^
  --add-data "tasks;tasks" ^
  --add-data "config.json;." ^
  --add-data "farms_template.json;." ^
  --collect-all easyocr ^
  --hidden-import adbutils ^
  --collect-all cv2 ^
  gui.py

echo Build complete. Output: dist\LastZBot.exe
