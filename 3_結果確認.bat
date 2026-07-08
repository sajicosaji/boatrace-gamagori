@echo off
chcp 65001 > nul
cd /d "%~dp0"

for /f %%d in ('powershell -command "Get-Date -Format yyyyMMdd"') do set TODAY=%%d

echo.
echo [BoatRace] ボートレース蒲郡 結果確認・回収率検証
echo ================================================
echo  当日の予想と実際の結果を照合して
echo  的中率・回収率（100円/点）を集計します
echo ================================================
echo.

set /p RACE_DATE="日付 (YYYYMMDD / Enter=今日): "
if "%RACE_DATE%"=="" set RACE_DATE=%TODAY%

set END_DATE=
set /p END_DATE="終了日 (期間検証する場合のみ入力 / Enter=1日分): "
echo.
set /p SEND_DISCORD="サマリーをDiscordに送信? (y/n): "
echo.

set OPTS=
if /i "%SEND_DISCORD%"=="y" set OPTS=--discord

if "%END_DATE%"=="" (
    python results.py %RACE_DATE% %OPTS%
) else (
    python results.py %RACE_DATE% %END_DATE% %OPTS%
)

echo.
pause
