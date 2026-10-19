@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist config mkdir config
if not exist strm mkdir strm
set "PORT=8025"
set "CONFIG_PATH=%~dp0config\config.json"
set "YUN139_OUTPUT_DIR=%~dp0strm"
echo 139Strm Pro starting: http://127.0.0.1:%PORT%
python app.py
pause
