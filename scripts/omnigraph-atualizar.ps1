# =============================================================================
#  omnigraph-atualizar — pega a ultima versao do OmniGraph, rapido. (Windows)
# =============================================================================
$ErrorActionPreference = "Continue"
$dir = if ($env:OMNIGRAPH_DIR) { $env:OMNIGRAPH_DIR } else { "$env:USERPROFILE\OmniGraph" }

if (-not (Test-Path "$dir\.git")) {
  Write-Host "Projeto nao encontrado em $dir." -ForegroundColor Yellow
  Write-Host 'Rode o instalador uma vez:'
  Write-Host '  powershell -c "irm https://raw.githubusercontent.com/Zespek/OmniGraph/main/instalar.ps1 | iex"'
  exit 1
}

Write-Host "> Atualizando o OmniGraph..." -ForegroundColor Magenta
git -C $dir fetch origin --quiet
git -C $dir reset --hard origin/main --quiet
Write-Host "  [ok] ultima versao baixada" -ForegroundColor Green

$bin = (uv tool dir --bin 2>$null); if (-not $bin) { $bin = "$env:USERPROFILE\.local\bin" }
Push-Location $dir
try { uv tool install --from ".[ollama,mcp,pdf,office,watch,sql]" omnigraph --force *> $null } finally { Pop-Location }
Write-Host "  [ok] ferramenta atualizada" -ForegroundColor Green

foreach ($c in @("omnigraph-mapa","omnigraph-perguntar","omnigraph-atualizar")) {
  foreach ($ext in @(".ps1",".cmd")) {
    $src = Join-Path $dir "scripts\$c$ext"
    if (Test-Path $src) { Copy-Item $src (Join-Path $bin "$c$ext") -Force -ErrorAction SilentlyContinue }
  }
}
Write-Host "  [ok] comandos atualizados" -ForegroundColor Green
Write-Host "> Pronto! Use: omnigraph-mapa  /  omnigraph-perguntar `"...`"" -ForegroundColor Green
