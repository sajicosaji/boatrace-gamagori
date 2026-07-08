@echo off
cd /d "%~dp0"

echo.
echo [BoatRace] 変更をGitHubに反映します...
echo.

git push origin master

echo.
echo 上にエラーが出ていなければ反映完了です。この窓は閉じてOK。
echo.
pause