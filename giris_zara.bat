@echo off
chcp 65001 >nul
title Zara Oturum Disa Aktarma
echo Zara tarayicisi aciliyor...
python export_session.py --store zara
echo.
pause
