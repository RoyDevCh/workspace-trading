@echo off
setlocal
set HTTP_PROXY=http://127.0.0.1:7897
set HTTPS_PROXY=http://127.0.0.1:7897
set PYTHONUTF8=1
cd /d "C:\Users\Roy\.openclaw\workspace"
"C:\Users\Roy\AppData\Local\Programs\Python\Python312\python.exe" "C:\Users\Roy\.openclaw\workspace\discord_bridge_daemon.py"
