# =============================================================================
#  Instalador do OmniGraph - Windows (PowerShell)
#
#  Como usar: clique com o botao direito neste arquivo > "Executar com PowerShell".
#  Se o Windows bloquear scripts, abra o PowerShell e rode:
#      powershell -ExecutionPolicy Bypass -File "Instalar OmniGraph.ps1"
#
#  Faz tudo sozinho: instala a ferramenta, verifica atualizacoes, instala a IA
#  local (Ollama) e baixa o modelo. Pode rodar de novo quando quiser.
# =============================================================================
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot

function Titulo($t) { Write-Host "`n> $t" -ForegroundColor Magenta }
function Ok($t)     { Write-Host "  [ok] $t" -ForegroundColor Green }
function Aviso($t)  { Write-Host "  [!]  $t" -ForegroundColor Yellow }

Write-Host "================================================"
Write-Host "   OmniGraph - instalacao e atualizacao (Windows)"
Write-Host "================================================"

# 1) uv (gerenciador que roda tudo) -------------------------------------------
Titulo "Preparando o ambiente"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  Aviso "instalando o uv..."
  powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
  $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}
if (Get-Command uv -ErrorAction SilentlyContinue) { Ok "uv pronto" } else { Aviso "nao consegui instalar o uv" }

# 2) verificar atualizacoes ---------------------------------------------------
Titulo "Verificando atualizacoes"
if ((Get-Command git -ErrorAction SilentlyContinue) -and (Test-Path .git)) {
  git fetch origin --quiet 2>$null
  if ((git rev-parse HEAD 2>$null) -eq (git rev-parse origin/main 2>$null)) {
    Ok "ja esta na ultima versao"
  } else {
    Aviso "atualizacao disponivel"
    $r = Read-Host "  Atualizar agora? [S/n]"
    # reset --hard resiste a historico reescrito (force-push) sem quebrar
    if ($r -ne "n" -and $r -ne "N") { git reset --hard origin/main --quiet; Ok "atualizado para a ultima versao" }
  }
}

# 3) instalar a ferramenta ----------------------------------------------------
Titulo "Instalando o OmniGraph"
# pasta onde o uv coloca o executavel (fonte da verdade)
$bin = (uv tool dir --bin 2>$null)
if (-not $bin) { $bin = "$env:USERPROFILE\.local\bin" }
$env:Path = "$bin;$env:Path"
$instLog = (uv tool install --from . omnigraph --force 2>&1 | Out-String)
uv tool update-shell 2>$null
if (Test-Path "$bin\omnigraph.exe") {
  Ok "comandos 'omnigraph' e 'omnigraph-mcp' instalados"
} else {
  Aviso "FALHA ao instalar a ferramenta. Detalhe do erro:"
  ($instLog -split "`n" | Select-Object -Last 12) | ForEach-Object { Write-Host "      $_" }
  Aviso "Copie essas linhas e mande para o suporte."
}

# 4) IA local (Ollama) - sem gastar tokens de API -----------------------------
Titulo "Configurando a IA local (Ollama)"
if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
  if (Get-Command winget -ErrorAction SilentlyContinue) {
    Aviso "instalando o Ollama via winget..."
    winget install --id Ollama.Ollama -e --accept-source-agreements --accept-package-agreements
    $env:Path = "$env:LOCALAPPDATA\Programs\Ollama;$env:Path"
  } else {
    Aviso "Ollama nao encontrado. Abrindo a pagina de download..."
    Aviso "Instale o Ollama e rode este instalador de novo."
    Start-Process "https://ollama.com/download"
  }
}
if (Get-Command ollama -ErrorAction SilentlyContinue) {
  Ok "Ollama instalado"
  try { Invoke-RestMethod "http://localhost:11434/api/tags" -TimeoutSec 2 | Out-Null }
  catch { Aviso "iniciando o servico do Ollama..."; Start-Process ollama -ArgumentList "serve" -WindowStyle Hidden; Start-Sleep 3 }
  $tem = $false
  try { $tags = Invoke-RestMethod "http://localhost:11434/api/tags" -TimeoutSec 3
        if ($tags.models.name -match "qwen2.5-coder:7b") { $tem = $true } } catch {}
  if ($tem) { Ok "modelo qwen2.5-coder:7b ja baixado" }
  else {
    Aviso "baixando o modelo qwen2.5-coder:7b (~4.7GB - pode demorar)..."
    ollama pull qwen2.5-coder:7b
    if ($LASTEXITCODE -eq 0) { Ok "modelo pronto" } else { Aviso "falha ao baixar o modelo" }
  }
}

# 5) PATH + IA local no ambiente do usuario -----------------------------------
Titulo "Ajustando o terminal (PATH e IA local)"
# garante a pasta dos comandos no PATH do usuario (persistente) para terminais novos
$pathUsuario = [Environment]::GetEnvironmentVariable("Path", "User")
if ($pathUsuario -notlike "*$bin*") {
  [Environment]::SetEnvironmentVariable("Path", "$bin;$pathUsuario", "User")
  Ok "PATH configurado (comando 'omnigraph' disponivel em terminais novos)"
} else { Ok "PATH ja configurado" }
setx OLLAMA_HOST "localhost:11434" | Out-Null
Ok "IA local definida como padrao"

# 6) verificacao final --------------------------------------------------------
Titulo "Verificando a instalacao"
$okInstalou = $false
if (Test-Path "$bin\omnigraph.exe") {
  try { & "$bin\omnigraph.exe" --version | Out-Null; $okInstalou = ($LASTEXITCODE -eq 0) } catch {}
}
if ($okInstalou) { Ok "OK! o comando 'omnigraph' respondeu" }
else { Aviso "o comando 'omnigraph' ainda nao respondeu (veja os erros acima)" }

Write-Host "`n================================================"
if ($okInstalou) { Ok "Tudo pronto!" } else { Aviso "Instalacao incompleta - leia as mensagens acima." }
Write-Host ""
Write-Host "  O comando 'omnigraph' funciona em QUALQUER pasta -"
Write-Host "  voce NAO precisa estar dentro da pasta do OmniGraph."
Write-Host ""
Aviso "Para usar, feche este terminal e abra um NOVO (carrega o PATH)."
Write-Host ""
Write-Host "  Depois, entre no SEU projeto e gere o mapa (os DOIS passos):"
Write-Host "      cd C:\caminho\do\seu\projeto"
Write-Host "      omnigraph extract . ; omnigraph cluster-only ."
Write-Host "  O grafico sai em:  omnigraph-out\graph.html"
Write-Host ""
Write-Host "  Instrucoes completas: abra 'guia de utilizacao\index.html'"
Write-Host "================================================`n"
Read-Host "Pressione Enter para fechar"
