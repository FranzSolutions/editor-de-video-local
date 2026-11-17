@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo.
echo === Instalando o Editor de Video Local ===
echo.

where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo [1/4] Instalando o ffmpeg...
  winget install --id Gyan.FFmpeg -e --source winget --accept-package-agreements --accept-source-agreements
) else (
  echo [1/4] ffmpeg ja instalado.
)

if not exist ".venv\Scripts\python.exe" (
  echo [2/4] Criando o ambiente Python...
  py -3 -m venv .venv 2>nul || python -m venv .venv
) else (
  echo [2/4] Ambiente Python ja existe.
)
if not exist ".venv\Scripts\python.exe" (
  echo.
  echo Nao encontrei o Python. Instale com:  winget install Python.Python.3.12
  echo e rode este instalar.bat de novo.
  pause
  exit /b 1
)

echo [3/4] Instalando as bibliotecas (pode demorar alguns minutos)...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\python.exe" -m pip install -r requirements.txt
where nvidia-smi >nul 2>nul
if not errorlevel 1 (
  echo       Placa NVIDIA encontrada: instalando o suporte a GPU...
  ".venv\Scripts\python.exe" -m pip install -r requirements-gpu.txt
)

echo [4/4] Baixando o modelo de transcricao (so na primeira vez)...
".venv\Scripts\python.exe" editar.py --baixar-modelo

echo.
echo Pronto! De dois cliques em "Abrir Editor.bat" para comecar.
pause
