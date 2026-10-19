@echo off
cd /d "%~dp0"
py -3 server.py
if errorlevel 1 (
  echo.
  echo Server tidak dapat dijalankan. Pastikan Python 3 sudah terpasang.
  pause
)
