@echo off
cd /d "%~dp0"

for /f %%d in ('powershell -command "Get-Date -Format yyyyMMdd"') do set TODAY=%%d

echo.
echo [BoatRace] ボートレース蒲郡 予想ツール
echo ========================================
echo  日付: Enter で今日 (%TODAY%)
echo  レース番号: 1～12 / Enter で全レース一括
echo ========================================
echo.

set /p RACE_DATE="日付 (YYYYMMDD / Enter=今日): "
if "%RACE_DATE%"=="" set RACE_DATE=%TODAY%

set RACE_NO=
set /p RACE_NO="レース番号 (1-12 / Enter=全レース): "
echo.
set /p SEND_DISCORD="Discordに送信? (y/n): "
echo.

set OPTS=
if /i "%SEND_DISCORD%"=="y" set OPTS=--discord

if "%RACE_NO%"=="" (
    python gamagori_race.py %RACE_DATE% --all %OPTS%
) else (
    python gamagori_race.py %RACE_DATE% %RACE_NO% %OPTS%
)

echo.
pause