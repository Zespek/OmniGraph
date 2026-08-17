@echo off
REM =============================================================================
REM  OmniGraph - instalador do Windows.
REM
REM  DE UM DUPLO-CLIQUE NESTE ARQUIVO. So isso.
REM
REM  Este arquivo se vira sozinho nos dois casos:
REM    - esta na pasta do OmniGraph (clone ou ZIP) -> instala dali;
REM    - esta sozinho (voce baixou so ele)         -> baixa o projeto e instala.
REM
REM  E um .cmd de proposito: arquivo .ps1 depende da politica de execucao do
REM  Windows e trava com "a execucao de scripts foi desabilitada neste sistema".
REM  O .cmd sempre roda, e chama o PowerShell com Bypass so para esta execucao -
REM  sem mexer na politica da maquina (nada de Set-ExecutionPolicy global).
REM =============================================================================
cd /d "%~dp0"
title Instalar OmniGraph

if exist "scripts\instalador-gui.ps1" goto :local

REM ---------------------------------------------------------------- sozinho --
REM Baixado avulso: pega o projeto e instala. O instalador abre a janela porque
REM OMNIGRAPH_GUI esta ligado.
echo.
echo   OmniGraph - baixando o projeto (so na primeira vez)...
echo.
set OMNIGRAPH_GUI=1
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/Zespek/OmniGraph/main/instalar.ps1 | iex"
if errorlevel 1 (
  echo.
  echo   Nao consegui baixar. Verifique a internet e tente de novo.
  pause
)
exit /b

REM ------------------------------------------------------------ pasta local --
:local
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\instalador-gui.ps1"

REM codigo 2 = este Windows nao abre janelas (Server Core, sessao remota sem GUI)
if errorlevel 2 (
  echo.
  echo Sem interface grafica neste Windows - seguindo pelo modo texto...
  echo.
  powershell -NoProfile -ExecutionPolicy Bypass -File "Instalar OmniGraph.ps1"
)
