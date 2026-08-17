@echo off
REM =============================================================================
REM  Atalho 'omnigraph-perguntar' para o Windows.
REM
REM  So o .cmd fica no PATH. O .ps1 mora em %LOCALAPPDATA%\OmniGraph\lib porque,
REM  se ficasse no PATH, o PowerShell escolheria ELE em vez deste atalho e o
REM  Windows barraria com "a execucao de scripts foi desabilitada neste sistema".
REM  Aqui o -ExecutionPolicy Bypass resolve isso sem mexer na politica da maquina.
REM =============================================================================
setlocal
set "OG_PS1=%LOCALAPPDATA%\OmniGraph\lib\omnigraph-perguntar.ps1"
if not exist "%OG_PS1%" set "OG_PS1=%~dp0omnigraph-perguntar.ps1"
powershell -NoProfile -ExecutionPolicy Bypass -File "%OG_PS1%" %*
endlocal & exit /b %ERRORLEVEL%
