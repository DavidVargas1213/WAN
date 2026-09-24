@echo off
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 servidor.py
) else (
  python servidor.py
)
pause
