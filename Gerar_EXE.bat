@echo off
setlocal
cd /d "%~dp0"
echo ==========================================
echo        LimpaVideo - Gerar executavel
echo ==========================================
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_windows.ps1"
if errorlevel 1 (
  echo.
  echo O build falhou. Confira as mensagens acima e o README.md.
  pause
  exit /b 1
)
echo.
echo Build concluido. O arquivo esta em dist\LimpaVideo.exe
pause
