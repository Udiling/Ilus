:: C:\Offline Server\update_oper.bat
@echo off
chcp 65001 >nul
cd /d "C:\Offline Server"
echo [OPER] Обновление справочника операторов...
python update_dict.py oper
echo Готово. Проверьте load.log и папку spr\archiv_oper\
pause
