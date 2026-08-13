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
  $atras = (git rev-list HEAD..origin/main --count 2>$null)
  if ($atras -and [int]$atras -gt 0) {
    Aviso "ha $atras atualizacao(oes) disponivel(is)"
    $r = Read-Host "  Atualizar agora? [S/n]"
    if ($r -ne "n" -and $r -ne "N") { git pull --ff-only origin main; Ok "atualizado para a ultima versao" }
  } else { Ok "ja esta na ultima versao" }
}

# 3) instalar a ferramenta ----------------------------------------------------
Titulo "Instalando o OmniGraph"
uv tool install --from . omnigraph --force
if ($LASTEXITCODE -eq 0) { Ok "comandos 'omnigraph' e 'omnigraph-mcp' instalados" } else { Aviso "falha ao instalar a ferramenta" }

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

# 5) usar a IA local por padrao -----------------------------------------------
Titulo "Definindo a IA local como padrao"
setx OLLAMA_HOST "localhost:11434" | Out-Null
Ok "IA local definida como padrao (reabra o terminal)"

Write-Host "`n================================================"
Ok "Tudo pronto!"
Write-Host "   - Abra um projeto no terminal e rode:  omnigraph extract ."
Write-Host "   - Instrucoes: abra 'guia de utilizacao\index.html'"
Write-Host "================================================`n"
Read-Host "Pressione Enter para fechar"
