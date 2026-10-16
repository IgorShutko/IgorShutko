@echo off
chcp 65001 >nul
cd /d %~dp0
echo === Установка Dolphin Autolaunch ===

where python >nul 2>nul
if errorlevel 1 (
  echo [!] Python не найден. Установи с https://www.python.org/downloads/
  echo     В установщике ОБЯЗАТЕЛЬНО поставь галку "Add python.exe to PATH".
  pause
  exit /b 1
)

if not exist .venv (
  echo Создаю окружение .venv ...
  python -m venv .venv || (echo [!] Не удалось создать .venv & pause & exit /b 1)
)

echo Ставлю зависимости ...
.venv\Scripts\python -m pip install -q --upgrade pip
.venv\Scripts\python -m pip install -q -r requirements.txt || (echo [!] Ошибка установки зависимостей & pause & exit /b 1)

if not exist config.json (
  copy config.example.json config.json >nul
  echo Создан config.json
)

echo.
echo === ГОТОВО ===
echo Сейчас откроется config.json в Блокноте.
echo Вставь токен Dolphin между кавычками в строке "dolphin_token", сохрани (Ctrl+S) и закрой.
pause
notepad config.json
echo.
echo Проверка токена - список профилей:
.venv\Scripts\python -m dolphin_ads profiles
pause
