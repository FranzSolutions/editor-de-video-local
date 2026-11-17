@echo off
chcp 65001 >nul
setlocal
title Editor de Video - deixe esta janela aberta
set "PASTA=%~dp0"
if not exist "%PASTA%.venv\Scripts\python.exe" (
  echo O editor ainda nao foi instalado. De dois cliques no instalar.bat primeiro.
  pause
  exit /b 1
)
"%PASTA%.venv\Scripts\python.exe" "%PASTA%painel.py" %*
if errorlevel 1 pause
