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
foreach ($a in $args) {
  if ($a -eq "--code-only") { $codeOnly = $true }
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

function RodarExtract($co, $mostrarErros) {
  $ogArgs = @("extract", $alvo)
  if ($co) { $ogArgs += "--code-only" }
  & $og @ogArgs 2>&1 | ForEach-Object {
    $line = "$_"
    if     ($line -match "scanning")               { Mostrar 5  "lendo os arquivos do projeto..." }
    elseif ($line -match "AST extraction on")      { Mostrar 15 "analisando o codigo..." }
    elseif ($line -match "semantic extraction on") { Mostrar 22 "a IA esta entendendo o projeto (pode levar minutos)..." }
    elseif ($line -match "chunk (\d+)/(\d+) done") {
      $x=[int]$Matches[1]; $y=[int]$Matches[2]
      if ($y -gt 0) { Mostrar (22 + [int]($x*63/$y)) "IA processando (parte $x de $y)..." }
    }
    elseif ($line -match "wrote.*graph\.json")     { Mostrar 90 "dados do mapa prontos" }
    elseif ($line -match "error:" -and $mostrarErros) { Write-Host $line -ForegroundColor Yellow }
  }
  return $LASTEXITCODE
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

# se a IA local nao esta de pe, ja vai direto pro modo codigo (sem erro feio)
if (-not $codeOnly -and -not (IaDisponivel)) {
  Write-Host "! IA local nao detectada - gerando o mapa direto do codigo (rapido)." -ForegroundColor Yellow
  $codeOnly = $true
}

Mostrar 0 "iniciando..."
# na 1a tentativa com IA nao mostro erros crus (ha fallback); no modo codigo, mostro
$ex = RodarExtract $codeOnly $codeOnly

if ($ex -ne 0 -and -not $codeOnly) {
  Write-Host "! A IA local falhou - gerando o mapa sem IA (modo seguro)..." -ForegroundColor Yellow
  $codeOnly = $true
  $ex = RodarExtract $true $true
}
if ($ex -ne 0) { Write-Progress -Activity "Gerando o mapa" -Completed; Write-Host "! Nao consegui extrair o mapa. Veja a mensagem acima." -ForegroundColor Yellow; exit 1 }

RodarCluster
Write-Progress -Activity "Gerando o mapa" -Completed
$html = Join-Path $alvo "omnigraph-out\graph.html"
Write-Host "OK Mapa pronto: $html" -ForegroundColor Green
if (Test-Path $html) { Start-Process $html }
