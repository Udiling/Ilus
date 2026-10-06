@echo off
chcp 65001 >nul
cd /d "C:\Offline Server"
echo ========================================
echo [QR-GEN] Автогенерация QR-кодов операторов
echo ========================================
python qr_gen_auto.py
echo.
echo [INFO] Изображения сохранены в: C:\Offline Server\static\qr_codes\
echo [INFO] Полный журнал: C:\Offline Server\load.log
echo.
pause