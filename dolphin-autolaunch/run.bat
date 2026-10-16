@echo off
REM Запуск команд: run.bat profiles / run.bat inspect --profile PROFILE_ID / run.bat launch brief.csv --dry-run
chcp 65001 >nul
cd /d %~dp0
if exist .venv\Scripts\python.exe (
  .venv\Scripts\python -m dolphin_ads %*
) else (
  echo Сначала запусти setup_windows.bat
)
