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
$lib = "$env:LOCALAPPDATA\OmniGraph\lib"
New-Item -ItemType Directory -Force -Path $lib | Out-Null
Push-Location $dir
try { uv tool install --from ".[ollama,mcp,pdf,office,watch,sql]" omnigraph --force *> $null } finally { Pop-Location }
Write-Host "  [ok] ferramenta atualizada" -ForegroundColor Green

# .ps1 no PATH faz o PowerShell chamar ELE em vez do atalho .cmd, e ai o Windows
# barra por politica de execucao. Entao: .cmd no PATH, .ps1 fora dele (em $lib).
Get-ChildItem $bin -Filter "omnigraph-*.ps1" -ErrorAction SilentlyContinue |
  ForEach-Object { Remove-Item $_.FullName -Force -ErrorAction SilentlyContinue }
Remove-Item "$bin\omnigraph-rag.py" -Force -ErrorAction SilentlyContinue

# copia TODOS os comandos (scripts\omnigraph-*); comando novo nunca fica de fora
Get-ChildItem (Join-Path $dir "scripts") -Filter "omnigraph-*" -ErrorAction SilentlyContinue |
  Where-Object { $_.Extension -in ".ps1",".cmd",".py" } |
  ForEach-Object {
    $destino = if ($_.Extension -eq ".cmd") { $bin } else { $lib }
    Copy-Item $_.FullName (Join-Path $destino $_.Name) -Force -ErrorAction SilentlyContinue
  }
Write-Host "  [ok] comandos atualizados" -ForegroundColor Green
Write-Host "> Pronto! Use: omnigraph-mapa  /  omnigraph-perguntar `"...`"" -ForegroundColor Green
