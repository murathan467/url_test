@echo off
chcp 65001 >nul
title Mango Oturum Disa Aktarma
echo Mango tarayicisi aciliyor...
python export_session.py --store mango
echo.
pause
