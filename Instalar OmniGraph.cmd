@echo off
REM =============================================================================
REM  OmniGraph - instalacao no Windows.
REM
REM  DE UM DUPLO-CLIQUE NESTE ARQUIVO. So isso.
REM
REM  E um .cmd de proposito: arquivo .ps1 depende da politica de execucao do
REM  Windows e trava com "a execucao de scripts foi desabilitada neste sistema".
REM  O .cmd sempre roda, e chama o PowerShell com Bypass so para esta execucao -
REM  sem mexer na politica da maquina (nada de Set-ExecutionPolicy global).
REM =============================================================================
cd /d "%~dp0"
title Instalar OmniGraph

powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\instalador-gui.ps1"

REM codigo 2 = este Windows nao abre janelas (Server Core, sessao remota sem GUI)
if errorlevel 2 (
  echo.
  echo Sem interface grafica neste Windows - seguindo pelo modo texto...
  echo.
  powershell -NoProfile -ExecutionPolicy Bypass -File "Instalar OmniGraph.ps1"
)
