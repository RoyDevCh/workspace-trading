@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo [1/4] Adding files...
git add -A

echo [2/4] Committing...
git commit -m "Update %date% %time:~0,8%"

echo [3/4] Pushing to GitHub...
git push

echo [4/4] Done!
pause
