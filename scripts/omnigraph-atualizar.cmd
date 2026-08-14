@echo off
REM Atalho para rodar 'omnigraph-atualizar' como comando no Windows.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0omnigraph-atualizar.ps1" %*
