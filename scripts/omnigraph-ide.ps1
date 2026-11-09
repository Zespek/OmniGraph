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

# mapa de um workspace: pasta acima com omnigraph-out (para antes da pasta pessoal)
$acima = $null
$d = (Resolve-Path $alvo).Path
while ($true) {
  $d = Split-Path $d -Parent
  if (-not $d -or $d -eq $HOME) { break }
  if (Test-Path (Join-Path $d "omnigraph-out\graph.json")) { $acima = $d; break }
}

# 1) garante o mapa
if (Test-Path (Join-Path $alvo "omnigraph-out\graph.json")) {
  Write-Host "  [ok] mapa ja existe (use 'omnigraph-mapa' para atualizar)" -ForegroundColor Green
} elseif ($acima) {
  Write-Host "  [ok] usando o mapa que ja existe em $acima" -ForegroundColor Green
} else {
  if ($mapa) { & $mapa $alvo }
  else { Push-Location $alvo; try { & $og extract . *> $null; & $og cluster-only . *> $null } finally { Pop-Location } }
}

# 2) registra no assistente/IDE
Write-Host "> Registrando o OmniGraph no seu assistente..." -ForegroundColor Magenta
Push-Location $alvo
try {
  & $og install
  # 'install' so grava a skill global; a regra no CLAUDE.md e o hook e que fazem o Claude Code usar o mapa
  # o plugin do Claude (VS Code, Cursor, Antigravity...) nao coloca 'claude' no PATH: procura a extensao tambem
  $extClaude = @("$HOME\.antigravity*\extensions\anthropic.claude-code-*", "$HOME\.vscode\extensions\anthropic.claude-code-*", "$HOME\.cursor\extensions\anthropic.claude-code-*", "$HOME\.windsurf\extensions\anthropic.claude-code-*") | Where-Object { Test-Path $_ }
  if ((Get-Command claude -ErrorAction SilentlyContinue) -or (Test-Path (Join-Path $HOME '.claude')) -or $extClaude) { & $og claude install --local | Out-Null }
  if ((Get-Command cursor -ErrorAction SilentlyContinue) -or (Test-Path (Join-Path $HOME '.cursor'))) { & $og cursor install --local | Out-Null }
  if ((Get-Command code -ErrorAction SilentlyContinue) -or (Test-Path (Join-Path $HOME '.vscode'))) { & $og vscode install --local | Out-Null }
  if ((Get-Command antigravity -ErrorAction SilentlyContinue) -or (Test-Path (Join-Path $HOME '.gemini\antigravity')) -or (Test-Path "$HOME\.antigravity*")) { & $og antigravity install --local | Out-Null }
  # 3) git hook: mantem o mapa atualizado sozinho a cada commit (se for repo git)
  if (Test-Path (Join-Path $alvo ".git")) {
    & $og hook install *> $null
    if ($LASTEXITCODE -eq 0) { Write-Host "  [ok] o mapa vai se atualizar sozinho a cada commit" -ForegroundColor Green }
  }
} finally { Pop-Location }

Write-Host "`n[ok] Pronto! Nao precisa deixar nenhum servidor rodando." -ForegroundColor Green
Write-Host "  Na sua IDE/assistente (Claude, Cursor, Copilot, VSCode...):"
Write-Host "    - pergunte sobre o projeto normalmente, ou digite  /omnigraph"
Write-Host "    - ele consulta o MAPA (poucos tokens) em vez de ler todos os arquivos"
Write-Host ""
Write-Host "  Atualizar o mapa manualmente (depois de mexer no codigo):  omnigraph update ."
