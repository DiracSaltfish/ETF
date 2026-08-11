@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  py -3 -m venv .venv
)
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements-windows-client.txt
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --windowed --onedir --add-data "assets\sounds;assets\sounds" --hidden-import PyQt6.QtMultimedia --name "ETF远程监控" etf_remote_client.py

echo 构建完成：dist\ETF远程监控\ETF远程监控.exe
pause
