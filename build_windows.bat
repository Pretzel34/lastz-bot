@echo off
REM Build the Windows standalone executable using PyInstaller.
REM Requires Python, pip, and PyInstaller installed.

pip install -r requirements.txt
pip install pyinstaller

REM Clean previous builds
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist LastZBot.spec del /q LastZBot.spec

pyinstaller --noconfirm --onefile --name LastZBot gui.py

echo Build complete. Output: dist\LastZBot.exe
