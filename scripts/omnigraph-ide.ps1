# =============================================================================
#  omnigraph-ide — prepara o projeto para usar com a IA da IDE gastando menos
#  tokens. Gera o mapa e registra o OmniGraph no assistente. (Windows)
#
#  Uso (na pasta do seu projeto):  omnigraph-ide
# =============================================================================
$ErrorActionPreference = "Continue"
$alvo = if ($args.Count -ge 1) { $args[0] } else { "." }

$og = (Get-Command omnigraph -ErrorAction SilentlyContinue).Source
if (-not $og) { $og = "$env:USERPROFILE\.local\bin\omnigraph.exe" }
if (-not (Test-Path $og)) { Write-Host "omnigraph nao encontrado. Rode o instalador primeiro."; exit 1 }
$mapa = (Get-Command omnigraph-mapa -ErrorAction SilentlyContinue).Source

Write-Host "> Preparando o projeto para a IA da sua IDE" -ForegroundColor Magenta

# 1) garante o mapa
if (Test-Path (Join-Path $alvo "omnigraph-out\graph.json")) {
  Write-Host "  [ok] mapa ja existe (use 'omnigraph-mapa' para atualizar)" -ForegroundColor Green
} else {
  if ($mapa) { & $mapa $alvo }
  else { Push-Location $alvo; try { & $og extract . *> $null; & $og cluster-only . *> $null } finally { Pop-Location } }
}

# 2) registra no assistente/IDE
Write-Host "> Registrando o OmniGraph no seu assistente..." -ForegroundColor Magenta
Push-Location $alvo
try { & $og install } finally { Pop-Location }

Write-Host "`n[ok] Pronto! Nao precisa deixar nenhum servidor rodando." -ForegroundColor Green
Write-Host "  Na sua IDE/assistente (Claude, Cursor, Copilot, VSCode...):"
Write-Host "    - pergunte sobre o projeto normalmente, ou digite  /omnigraph"
Write-Host "    - ele consulta o MAPA (poucos tokens) em vez de ler todos os arquivos"
Write-Host ""
Write-Host "  Mexeu bastante no codigo? Rode 'omnigraph-mapa' de novo para atualizar o mapa."
