@echo off
chcp 65001 >nul
title Mango Oturum Disa Aktarma
echo Mango tarayicisi aciliyor (giris sonrasi GitHub Secret otomatik guncellenecek)...
python export_session.py --store mango --upload-secret
echo.
pause
