@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" -Mode demo -OllamaModel qwen3:4b -VisionModel qwen3-vl:2b-instruct
pause
