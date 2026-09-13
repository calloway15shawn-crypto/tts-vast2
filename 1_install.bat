@echo off
chcp 65001 >nul
cd /d "%~dp0"
py -m pip install -r requirements.txt
if not exist config.yaml copy config.example.yaml config.yaml >nul
echo.
echo Установка завершена. Откройте config.yaml в Блокноте и заполните его.
pause
