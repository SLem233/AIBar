@echo off
rem Launch AIBar without a console window
cd /d "%~dp0"
start "" pythonw -m aibar.main
