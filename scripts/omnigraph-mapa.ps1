# =============================================================================
#  omnigraph-mapa — gera o mapa do projeto mostrando o progresso em % (Windows).
#
#  Uso (na pasta do seu projeto):
#      omnigraph-mapa
#      omnigraph-mapa --code-only
#      omnigraph-mapa C:\caminho\do\projeto
# =============================================================================
$ErrorActionPreference = "Continue"

$alvo = "."
$codeOnly = $false
$incluirTudo = $false
foreach ($a in $args) {
  if     ($a -eq "--code-only") { $codeOnly = $true }
  elseif ($a -eq "--tudo" -or $a -eq "--no-gitignore") { $incluirTudo = $true }
  elseif ($a -notlike "-*") { $alvo = $a }
}

$og = (Get-Command omnigraph -ErrorAction SilentlyContinue).Source
if (-not $og) { $og = "$env:USERPROFILE\.local\bin\omnigraph.exe" }
if (-not (Test-Path $og)) { Write-Host "omnigraph nao encontrado. Rode o instalador primeiro."; exit 1 }

function Mostrar($p, $label) { Write-Progress -Activity "Gerando o mapa" -Status "$p% - $label" -PercentComplete $p }

function IaDisponivel {
  $h = if ($env:OLLAMA_HOST) { $env:OLLAMA_HOST } else { "localhost:11434" }
  try { Invoke-RestMethod "http://$h/api/tags" -TimeoutSec 3 | Out-Null; return $true } catch { return $false }
}

# se a IA estiver parada mas instalada, SOBE ela sozinha (o usuario nao precisa saber)
function GarantirOllama {
  if (IaDisponivel) { return $true }
  if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) { return $false }
  Write-Host "> iniciando a IA local (Ollama), aguarde..." -ForegroundColor Magenta
  Start-Process ollama -ArgumentList "serve" -WindowStyle Hidden -ErrorAction SilentlyContinue
  for ($i=0; $i -lt 30; $i++) { if (IaDisponivel) { return $true }; Start-Sleep 1 }
  return $false
}

$script:LOG = Join-Path $env:TEMP ("omnigraph_mapa_" + $PID + ".log")

function RodarExtract($co) {
  $ogArgs = @("extract", $alvo)
  if ($co) { $ogArgs += "--code-only" }
  if ($incluirTudo) { $ogArgs += "--no-gitignore" }
  & $og @ogArgs 2>&1 | Tee-Object -FilePath $script:LOG | ForEach-Object {
    $line = "$_"
    if     ($line -match "scanning")               { Mostrar 5  "lendo os arquivos do projeto..." }
    elseif ($line -match "AST extraction on")      { Mostrar 15 "analisando o codigo..." }
    elseif ($line -match "semantic extraction on") { Mostrar 22 "a IA esta entendendo o projeto (pode levar minutos)..." }
    elseif ($line -match "chunk (\d+)/(\d+) done") {
      $x=[int]$Matches[1]; $y=[int]$Matches[2]
      if ($y -gt 0) { Mostrar (22 + [int]($x*63/$y)) "IA processando (parte $x de $y)..." }
    }
    elseif ($line -match "wrote.*graph\.json")     { Mostrar 90 "dados do mapa prontos" }
  }
  return $LASTEXITCODE
}

function MostrarErroReal {
  Write-Host "`nErro real do extract:" -ForegroundColor Yellow
  if (Test-Path $script:LOG) { Get-Content $script:LOG -Tail 25 | ForEach-Object { Write-Host "    $_" } }
}

function RodarCluster {
  & $og cluster-only $alvo 2>&1 | ForEach-Object {
    $line = "$_"
    if     ($line -match "Re-clustering") { Mostrar 93 "montando o grafico..." }
    elseif ($line -match "Labeling")      { Mostrar 96 "nomeando as areas..." }
    elseif ($line -match "Done")          { Mostrar 100 "pronto!" }
  }
}

Write-Host "> Gerando o mapa de: $alvo" -ForegroundColor Magenta

# tenta garantir a IA de pe (subindo o Ollama se preciso); senao, modo codigo
if (-not $codeOnly -and -not (GarantirOllama)) {
  Write-Host "! IA local nao detectada - gerando o mapa direto do codigo (rapido)." -ForegroundColor Yellow
  $codeOnly = $true
}

Mostrar 0 "iniciando..."
$ex = RodarExtract $codeOnly

if ($ex -ne 0 -and -not $codeOnly) {
  Write-Host "! A IA local falhou - gerando o mapa sem IA (modo seguro)..." -ForegroundColor Yellow
  $codeOnly = $true
  $ex = RodarExtract $true
}
if ($ex -ne 0) {
  Write-Progress -Activity "Gerando o mapa" -Completed
  $vazio = (Test-Path $script:LOG) -and (Select-String -Path $script:LOG -Pattern "graph is empty|found 0 code, 0 docs" -Quiet)
  if ($vazio) {
    Write-Host "! Nenhum arquivo de projeto reconhecido nesta pasta." -ForegroundColor Yellow
    Write-Host "  Voce esta na pasta certa? Ela precisa conter o codigo-fonte."
    Write-Host "    - veja o que tem aqui com:   dir"
    Write-Host "    - se o codigo esta numa subpasta, entre nela e rode de novo"
    Write-Host "    - se os arquivos existem mas ha um .gitignore, tente incluir tudo:"
    Write-Host "        omnigraph-mapa --tudo"
  } else {
    Write-Host "! Nao consegui gerar o mapa." -ForegroundColor Yellow
    MostrarErroReal
    Write-Host "`nCopie o 'Erro real' acima e me mande." -ForegroundColor Yellow
  }
  Remove-Item $script:LOG -ErrorAction SilentlyContinue
  exit 1
}
Remove-Item $script:LOG -ErrorAction SilentlyContinue

RodarCluster
Write-Progress -Activity "Gerando o mapa" -Completed
$html = Join-Path $alvo "omnigraph-out\graph.html"

# grafo grande (>5000 nos): o cluster-only pula o graph.html; gera a visao agregada
if (-not (Test-Path $html)) {
  Write-Host "> projeto grande: gerando a visao por comunidades..." -ForegroundColor Magenta
  Push-Location $alvo; try { & $og export html *> $null } finally { Pop-Location }
}

if (Test-Path $html) {
  Write-Host "OK Mapa pronto: $html" -ForegroundColor Green
  Start-Process $html
} else {
  Write-Host "! Os dados do mapa foram gerados, mas nao consegui criar o graph.html." -ForegroundColor Yellow
  Write-Host "  Tente, na pasta do projeto:  omnigraph export html"
}
