@echo off
chcp 65001 >nul
title Zara Oturum Disa Aktarma
echo Zara tarayicisi aciliyor (giris sonrasi GitHub Secret otomatik guncellenecek)...
python export_session.py --store zara --upload-secret
echo.
pause
