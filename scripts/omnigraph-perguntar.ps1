# =============================================================================
#  omnigraph-perguntar — pergunte, em portugues, como algo do projeto funciona.
#  Usa a IA local sobre o mapa ja gerado. (Windows)
#
#  Uso (na pasta do seu projeto):
#      omnigraph-perguntar "como funciona o pagamento?"
#      omnigraph-perguntar "onde o login valida a senha?" C:\caminho\projeto
# =============================================================================
$ErrorActionPreference = "Continue"

$pergunta = if ($args.Count -ge 1) { $args[0] } else { "" }
$alvo     = if ($args.Count -ge 2) { $args[1] } else { "." }

if (-not $pergunta) {
  Write-Host 'Uso: omnigraph-perguntar "sua pergunta" [pasta do projeto]'
  Write-Host 'Ex.: omnigraph-perguntar "como funciona o pagamento?"'
  exit 1
}

$og = (Get-Command omnigraph -ErrorAction SilentlyContinue).Source
if (-not $og) { $og = "$env:USERPROFILE\.local\bin\omnigraph.exe" }
if (-not (Test-Path $og)) { Write-Host "omnigraph nao encontrado. Rode o instalador primeiro."; exit 1 }

if (-not (Test-Path (Join-Path $alvo "omnigraph-out\graph.json"))) {
  Write-Host "Ainda nao ha um mapa nesta pasta." -ForegroundColor Yellow
  Write-Host "Gere o mapa primeiro (na pasta do projeto):"
  Write-Host "    omnigraph-mapa"
  exit 1
}

Write-Host "> Pergunta: $pergunta" -ForegroundColor Magenta
Write-Progress -Activity "Perguntando a IA local" -Status "a IA esta pensando..."
Push-Location $alvo
try {
  & $og query $pergunta 2>&1 | ForEach-Object { Write-Host $_ }
  $rc = $LASTEXITCODE
} finally {
  Pop-Location
  Write-Progress -Activity "Perguntando a IA local" -Completed
}
if ($rc -ne 0) {
  Write-Host "Se falar em IA/backend, confirme a IA local rodando o instalador de novo," -ForegroundColor Yellow
  Write-Host "ou gere o mapa sem IA com:  omnigraph-mapa --code-only" -ForegroundColor Yellow
}
