# =============================================================================
#  Instalador do OmniGraph - Windows (PowerShell)
#
#  Como usar: de um duplo-clique em "Instalar OmniGraph.cmd" (abre a interface).
#  Prefere o terminal? Rode:
#      powershell -ExecutionPolicy Bypass -File "Instalar OmniGraph.ps1"
#
#  Faz tudo sozinho: instala a ferramenta, verifica atualizacoes, instala a IA
#  local (Ollama) e baixa o modelo. Pode rodar de novo quando quiser.
# =============================================================================
param(
  # sem perguntas e sem pausa no final (usado pela interface grafica)
  [switch]$Auto,
  # pula o Ollama e os modelos (instalacao leve, sem baixar GBs)
  [switch]$SemIA
)
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot

function Titulo($t) { Write-Host "`n> $t" -ForegroundColor Magenta }
function Ok($t)     { Write-Host "  [ok] $t" -ForegroundColor Green }
function Aviso($t)  { Write-Host "  [!]  $t" -ForegroundColor Yellow }

# Identidade do OmniGraph: a mesma caveira roxa que o 'omnigraph install' mostra.
# As here-strings sao de aspas SIMPLES porque o desenho e cheio de crases, e entre
# aspas duplas o PowerShell as leria como escape e comeria pedacos da arte.
function Arte {
  $caveira = @'
          .                                                      .
        .n                   .                 .                  n.
  .   .dP                  dP                   9b                 9b.    .
 4    qXb         .       dX                     Xb       .        dXp     t
dX.    9Xb      .dXb    __                         __    dXb.     dXP     .Xb
9XXb._       _.dXXXXb dXXXXbo.                 .odXXXXb dXXXXb._       _.dXXP
 9XXXXXXXXXXXXXXXXXXXVXXXXXXXXOo.           .oOXXXXXXXXVXXXXXXXXXXXXXXXXXXXP
  `9XXXXXXXXXXXXXXXXXXXXX'~   ~`OOO8b   d8OOO'~   ~`XXXXXXXXXXXXXXXXXXXXXP'
    `9XXXXXXXXXXXP' `9XX'   DIE    `98v8P'  HUMAN   `XXP' `9XXXXXXXXXXXP'
        ~~~~~~~       9X.          .db|db.          .XP       ~~~~~~~
                        )b.  .dbo.dP'`v'`9b.odb.  .dX(
                      ,dXXXXXXXXXXXb     dXXXXXXXXXXXb.
                     dXXXXXXXXXXXP'   .   `9XXXXXXXXXXXb
                    dXXXXXXXXXXXXb   d|b   dXXXXXXXXXXXXb
                    9XXb'   `XXXXXb.dX|Xb.dXXXXX'   `dXXP
                     `'      9XXXXXX(   )XXXXXXP      `'
                              XXXX X.`v'.X XXXX
                              XP^X'`b   d'`X^XX
                              X. 9  `   '  P )X
                              `b  `       '  d'
                               `             '
'@
  # nome em blocos, como no 'omnigraph install'. Se o console nao aceitar os
  # blocos (codepage antiga), eles virariam '?' - ai vale mais o nome simples.
  $nome = @'
  █▀█ █▀▄▀█ █▄ █ █ █▀▀ █▀█ ▄▀█ █▀█ █ █
  █▄█ █ ▀ █ █ ▀█ █ █▄█ █▀▄ █▀█ █▀▀ █▀█
'@
  try {
    $blocos = ([char]0x2588).ToString() + ([char]0x2580) + ([char]0x2584)
    $volta = [Console]::OutputEncoding.GetString([Console]::OutputEncoding.GetBytes($blocos))
    if ($volta -ne $blocos) { $nome = "  O M N I G R A P H" }
  } catch { $nome = "  O M N I G R A P H" }

  # a caveira so cabe em janela larga; na estreita fica so o nome
  $largura = 80
  try { $largura = $Host.UI.RawUI.WindowSize.Width } catch {}
  $desenho = if ($largura -ge 80) { $caveira + "`n" + $nome } else { $nome }

  # Windows Terminal e VSCode aceitam cor de 24 bits (o roxo exato #bd00ff); no
  # console classico o Magenta do proprio PowerShell faz o papel do roxo
  if ($env:WT_SESSION -or $env:TERM_PROGRAM) {
    Write-Host ("$([char]27)[1;38;2;189;0;255m" + $desenho + "$([char]27)[0m")
  } else {
    Write-Host $desenho -ForegroundColor Magenta
  }
}

# no modo -Auto quem mostra a arte e a janela do instalador, com a mesma caveira
if (-not $Auto) { Arte }
Write-Host "  instalacao e atualizacao (Windows)`n" -ForegroundColor DarkGray

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
    $r = if ($Auto) { "s" } else { Read-Host "  Atualizar agora? [S/n]" }
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
# comandos amigaveis: o .cmd (atalho) vai para o PATH e o .ps1 (o codigo de
# verdade) fica FORA dele, em %LOCALAPPDATA%\OmniGraph\lib. Se o .ps1 ficasse no
# PATH, o PowerShell chamaria ele direto e o Windows barraria com "a execucao de
# scripts foi desabilitada neste sistema"; pelo .cmd, o Bypass ja vem junto.
$lib = "$env:LOCALAPPDATA\OmniGraph\lib"
New-Item -ItemType Directory -Force -Path $lib | Out-Null

# limpa os .ps1 que versoes antigas deixaram no PATH (a causa do erro acima)
Get-ChildItem $bin -Filter "omnigraph-*.ps1" -ErrorAction SilentlyContinue |
  ForEach-Object { Remove-Item $_.FullName -Force -ErrorAction SilentlyContinue }
Remove-Item "$bin\omnigraph-rag.py" -Force -ErrorAction SilentlyContinue

$comandos = @{
  "omnigraph-mapa"       = "gera o mapa mostrando o progresso"
  "omnigraph-perguntar"  = "pergunte como algo funciona"
  "omnigraph-atualizar"  = "atualiza tudo rapido"
  "omnigraph-ide"        = "usa na IDE gastando menos tokens"
}
foreach ($nome in $comandos.Keys) {
  if (-not (Test-Path "scripts\$nome.ps1")) { continue }
  Copy-Item "scripts\$nome.ps1" "$lib\$nome.ps1" -Force -ErrorAction SilentlyContinue
  Copy-Item "scripts\$nome.cmd" "$bin\$nome.cmd" -Force -ErrorAction SilentlyContinue
  if (Test-Path "$bin\$nome.cmd") { Ok "comando '$nome' instalado ($($comandos[$nome]))" }
}
# motor de busca semantica (usado pelo omnigraph-perguntar)
if (Test-Path "scripts\omnigraph-rag.py") { Copy-Item "scripts\omnigraph-rag.py" "$lib\omnigraph-rag.py" -Force -ErrorAction SilentlyContinue }

# 4) IA local (Ollama) - sem gastar tokens de API -----------------------------
$iaOk = $false
if ($SemIA) {
  Titulo "IA local"
  Aviso "pulando o Ollama e os modelos (voce escolheu a instalacao leve)"
  Aviso "o mapa sai direto do codigo; para ativar a IA depois, rode o instalador de novo"
} else {
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
    # modelo de EMBEDDINGS: busca semantica do omnigraph-perguntar (casa portugues com codigo em ingles)
    $temEmb = $false
    try { $tags = Invoke-RestMethod "http://localhost:11434/api/tags" -TimeoutSec 3
          if ($tags.models.name -match "bge-m3") { $temEmb = $true } } catch {}
    if ($temEmb) { Ok "busca semantica (bge-m3) ja baixada" }
    else {
      Aviso "baixando o modelo de busca semantica bge-m3 (~1.2GB)..."
      ollama pull bge-m3
      if ($LASTEXITCODE -eq 0) { Ok "busca semantica pronta" } else { Aviso "falha no bge-m3 (perguntar usara a busca do grafo)" }
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
} elseif ($SemIA) {
  # instalacao leve: nao mexe no que ja estava configurado (quem ja tinha a IA
  # rodando nao pode perde-la so por desmarcar a caixinha)
  Ok "IA local nao alterada (instalacao leve)"
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

# Confere o que o PowerShell REALMENTE vai chamar. Se sobrar um .ps1 solto em
# alguma pasta do PATH, ele sequestra o comando e o usuario leva o erro de
# politica de execucao mesmo com a instalacao "concluida" - foi assim que o
# problema passou batido antes.
$sequestrado = @()
foreach ($nome in $comandos.Keys) {
  $achado = (Get-Command $nome -ErrorAction SilentlyContinue | Select-Object -First 1)
  if ($achado -and $achado.Source -like "*.ps1") { $sequestrado += $achado.Source }
}
if ($sequestrado) {
  Aviso "sobrou um .ps1 antigo no PATH - ele bloqueia os comandos:"
  $sequestrado | ForEach-Object { Write-Host "      $_" }
  $sequestrado | ForEach-Object { Remove-Item $_ -Force -ErrorAction SilentlyContinue }
  $aindaLa = $sequestrado | Where-Object { Test-Path $_ }
  if ($aindaLa) {
    Aviso "nao consegui apagar. Apague esses arquivos a mao e rode o instalador de novo."
    $okInstalou = $false
  } else { Ok "arquivos antigos removidos - os comandos estao livres" }
} else { Ok "comandos apontando para os atalhos .cmd (nao dependem da politica do Windows)" }

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
if (-not $iaOk -and -not $SemIA) {
  Write-Host ""
  Write-Host "  Obs.: a IA local nao ficou ativa - o 'omnigraph-mapa' gera o mapa direto do"
  Write-Host "  codigo automaticamente (sem erro). Para ativar a IA depois, instale o Ollama"
  Write-Host "  e rode este instalador de novo."
}
Write-Host "  O 'omnigraph-mapa' mostra a barra e abre o grafico (omnigraph-out\graph.html) sozinho."
Write-Host ""
Write-Host "  Instrucoes completas: abra 'guia de utilizacao\index.html'"
Write-Host "================================================`n"
# no modo -Auto quem mostra o resultado e a interface grafica; pausar travaria ela
if (-not $Auto) { Read-Host "Pressione Enter para fechar" }
if (-not $okInstalou) { exit 1 }
