@echo off
REM Пример: run.bat launch examples\brief.csv --dry-run
cd /d %~dp0
python -m dolphin_ads %*
