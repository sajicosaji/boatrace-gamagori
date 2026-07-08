@echo off
cd /d "%~dp0"
echo [BoatRace] 本日の蒲郡全レースをDiscordに自動送信
echo   締切10分前に各レースを順番に送信します
echo   Ctrl+C で中断できます
echo.
python daily_discord.py %*
echo.
pause