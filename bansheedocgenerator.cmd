@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
python -m bansheedocgenerator %*
endlocal
