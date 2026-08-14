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
# [ollama] traz o pacote 'openai' que o backend local exige; os demais habilitam
# PDFs, docs, .sql, watch e a integracao com assistentes (mcp). Todos tem wheel.
$instLog = (uv tool install --from ".[ollama,mcp,pdf,office,watch,sql]" omnigraph --force 2>&1 | Out-String)
uv tool update-shell 2>$null
if (Test-Path "$bin\omnigraph.exe") {
  Ok "comandos 'omnigraph' e 'omnigraph-mcp' instalados"
} else {
  Aviso "FALHA ao instalar a ferramenta. Detalhe do erro:"
  ($instLog -split "`n" | Select-Object -Last 12) | ForEach-Object { Write-Host "      $_" }
  Aviso "Copie essas linhas e mande para o suporte."
}
# comandos amigaveis: barra de progresso e perguntar em portugues
if (Test-Path "scripts\omnigraph-mapa.ps1") {
  Copy-Item "scripts\omnigraph-mapa.ps1" "$bin\omnigraph-mapa.ps1" -Force -ErrorAction SilentlyContinue
  Copy-Item "scripts\omnigraph-mapa.cmd" "$bin\omnigraph-mapa.cmd" -Force -ErrorAction SilentlyContinue
  if (Test-Path "$bin\omnigraph-mapa.cmd") { Ok "comando 'omnigraph-mapa' instalado (gera o mapa mostrando o progresso)" }
}
if (Test-Path "scripts\omnigraph-perguntar.ps1") {
  Copy-Item "scripts\omnigraph-perguntar.ps1" "$bin\omnigraph-perguntar.ps1" -Force -ErrorAction SilentlyContinue
  Copy-Item "scripts\omnigraph-perguntar.cmd" "$bin\omnigraph-perguntar.cmd" -Force -ErrorAction SilentlyContinue
  if (Test-Path "$bin\omnigraph-perguntar.cmd") { Ok "comando 'omnigraph-perguntar' instalado (pergunte como algo funciona)" }
}
if (Test-Path "scripts\omnigraph-atualizar.ps1") {
  Copy-Item "scripts\omnigraph-atualizar.ps1" "$bin\omnigraph-atualizar.ps1" -Force -ErrorAction SilentlyContinue
  Copy-Item "scripts\omnigraph-atualizar.cmd" "$bin\omnigraph-atualizar.cmd" -Force -ErrorAction SilentlyContinue
  if (Test-Path "$bin\omnigraph-atualizar.cmd") { Ok "comando 'omnigraph-atualizar' instalado (atualiza tudo rapido)" }
}
if (Test-Path "scripts\omnigraph-ide.ps1") {
  Copy-Item "scripts\omnigraph-ide.ps1" "$bin\omnigraph-ide.ps1" -Force -ErrorAction SilentlyContinue
  Copy-Item "scripts\omnigraph-ide.cmd" "$bin\omnigraph-ide.cmd" -Force -ErrorAction SilentlyContinue
  if (Test-Path "$bin\omnigraph-ide.cmd") { Ok "comando 'omnigraph-ide' instalado (usa na IDE gastando menos tokens)" }
}

# 4) IA local (Ollama) - sem gastar tokens de API -----------------------------
Titulo "Configurando a IA local (Ollama)"
if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
  if (Get-Command winget -ErrorAction SilentlyContinue) {
    Aviso "instalando o Ollama via winget..."
    winget install --id Ollama.Ollama -e --accept-source-agreements --accept-package-agreements
  } else {
    # sem winget: baixa e roda o instalador oficial em silencio (automatico)
    Aviso "baixando e instalando o Ollama automaticamente..."
    try {
      $exe = Join-Path $env:TEMP "OllamaSetup.exe"
      Invoke-WebRequest "https://ollama.com/download/OllamaSetup.exe" -OutFile $exe -UseBasicParsing
      Start-Process $exe -ArgumentList "/VERYSILENT","/NORESTART" -Wait
    } catch {
      Aviso "nao consegui instalar automaticamente; abrindo a pagina de download..."
      Start-Process "https://ollama.com/download"
    }
  }
  $env:Path = "$env:LOCALAPPDATA\Programs\Ollama;$env:Path"
}
$iaOk = $false
if (Get-Command ollama -ErrorAction SilentlyContinue) {
  Ok "Ollama instalado"
  # garante o servidor no ar (o app do Ollama no Windows ja sobe sozinho no login)
  try { Invoke-RestMethod "http://localhost:11434/api/tags" -TimeoutSec 2 | Out-Null }
  catch { Aviso "iniciando o Ollama..."; Start-Process ollama -ArgumentList "serve" -WindowStyle Hidden }
  for ($i=0; $i -lt 20; $i++) {
    try { Invoke-RestMethod "http://localhost:11434/api/tags" -TimeoutSec 2 | Out-Null; break } catch { Start-Sleep 1 }
  }
  # escolhe o modelo pela RAM da maquina: o mais forte que ela aguenta
  $ramGb = [math]::Floor((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB)
  if     ($ramGb -ge 30) { $modelo = "qwen2.5-coder:32b"; $tam = "~20GB" }
  elseif ($ramGb -ge 15) { $modelo = "qwen2.5-coder:14b"; $tam = "~9GB" }
  elseif ($ramGb -ge 7)  { $modelo = "qwen2.5-coder:7b";  $tam = "~4.7GB" }
  else                   { $modelo = "qwen2.5-coder:3b";  $tam = "~2GB" }
  Ok "RAM detectada: ${ramGb}GB  ->  modelo $modelo (o mais forte para esta maquina)"

  $tem = $false
  try { $tags = Invoke-RestMethod "http://localhost:11434/api/tags" -TimeoutSec 3
        if ($tags.models.name -match [regex]::Escape($modelo)) { $tem = $true } } catch {}
  if ($tem) { Ok "modelo $modelo ja baixado" }
  else {
    Aviso "baixando o modelo $modelo ($tam - pode demorar)..."
    ollama pull $modelo
    if ($LASTEXITCODE -eq 0) { Ok "modelo pronto" } else { Aviso "falha ao baixar o modelo" }
  }
  # TESTE REAL de ponta a ponta: mini-extract com Ollama (passa pelo pacote 'openai').
  Aviso "testando a IA local de verdade (pode levar ~30s na 1a vez)..."
  $env:OLLAMA_HOST = "localhost:11434"; $env:OLLAMA_API_KEY = "ollama"; $env:OLLAMA_MODEL = $modelo
  $smoke = Join-Path $env:TEMP ("omnigraph_smoke_" + $PID)
  New-Item -ItemType Directory -Force -Path $smoke | Out-Null
  "# Modulo de Pagamento`n`nEste modulo processa pagamentos e conversa com o Banco de Dados." | Set-Content -Encoding UTF8 (Join-Path $smoke "exemplo.md")
  & "$bin\omnigraph.exe" extract $smoke --backend ollama *> $null
  if ($LASTEXITCODE -eq 0) { Ok "IA local respondeu - pronta para uso"; $iaOk = $true }
  else {
    Aviso "a IA local NAO respondeu ao teste (o mapa ainda funciona sem ela)."
    Aviso "Use o modo sem IA:  omnigraph extract . --code-only"
  }
  Remove-Item -Recurse -Force $smoke -ErrorAction SilentlyContinue
}

# 5) PATH + IA local no ambiente do usuario -----------------------------------
Titulo "Ajustando o terminal (PATH e IA local)"
# garante a pasta dos comandos no PATH do usuario (persistente) para terminais novos
$pathUsuario = [Environment]::GetEnvironmentVariable("Path", "User")
if ($pathUsuario -notlike "*$bin*") {
  [Environment]::SetEnvironmentVariable("Path", "$bin;$pathUsuario", "User")
  Ok "PATH configurado (comando 'omnigraph' disponivel em terminais novos)"
} else { Ok "PATH ja configurado" }
# So aponta para a IA local se ela passou no teste (senao o tool nem tenta o
# Ollama e usa so o codigo, evitando erro fatal em quem nao tem a IA).
if ($iaOk) {
  setx OLLAMA_HOST "localhost:11434" | Out-Null
  setx OLLAMA_API_KEY "ollama" | Out-Null
  if ($modelo) { setx OLLAMA_MODEL $modelo | Out-Null }
  Ok "IA local definida como padrao"
} else {
  [Environment]::SetEnvironmentVariable("OLLAMA_HOST", $null, "User")
  Ok "IA local nao ativada (o mapa vai funcionar so com o codigo)"
}

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
Write-Host "  Depois, entre no SEU projeto e use estes comandos:"
Write-Host "      cd C:\caminho\do\seu\projeto"
Write-Host "      omnigraph-mapa                                 # gera o mapa (com % de progresso)"
Write-Host '      omnigraph-perguntar "como funciona o login?"   # pergunta em portugues'
if (-not $iaOk) {
  Write-Host ""
  Write-Host "  Obs.: a IA local nao ficou ativa - o 'omnigraph-mapa' gera o mapa direto do"
  Write-Host "  codigo automaticamente (sem erro). Para ativar a IA depois, instale o Ollama"
  Write-Host "  e rode este instalador de novo."
}
Write-Host "  O 'omnigraph-mapa' mostra a barra e abre o grafico (omnigraph-out\graph.html) sozinho."
Write-Host ""
Write-Host "  Instrucoes completas: abra 'guia de utilizacao\index.html'"
Write-Host "================================================`n"
Read-Host "Pressione Enter para fechar"
