:: C:\Offline Server\update_menu.bat
@echo off
chcp 65001 >nul
echo Загрузка и архивация меню...
python "C:\Offline Server\update_dict.py" menu && pause
echo Готово. Проверь menu_load.log и папку arch\menu при необходимости.
pause