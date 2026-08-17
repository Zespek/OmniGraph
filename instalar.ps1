# =============================================================================
#  OmniGraph - instalacao em um comando (Windows). Sem ZIP.
#
#  Uso (cole no PowerShell):
#    powershell -c "irm https://raw.githubusercontent.com/Zespek/OmniGraph/main/instalar.ps1 | iex"
#
#  Clona o projeto em %USERPROFILE%\OmniGraph e roda o instalador completo
#  (ferramenta + IA local + modelo). Rodar de novo apenas atualiza.
# =============================================================================
$ErrorActionPreference = "Stop"
$repo = "https://github.com/Zespek/OmniGraph.git"
$dir = if ($env:OMNIGRAPH_DIR) { $env:OMNIGRAPH_DIR } else { "$env:USERPROFILE\OmniGraph" }

Write-Host "OmniGraph  ->  instalando em: $dir"

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
  # tenta resolver sozinho: exigir que a pessoa instale o git a mao trava aqui
  if (Get-Command winget -ErrorAction SilentlyContinue) {
    Write-Host "git nao encontrado - instalando pelo winget..."
    winget install --id Git.Git -e --source winget --accept-source-agreements --accept-package-agreements
    $env:Path = "$env:ProgramFiles\Git\cmd;$env:Path"
  }
}
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
  Write-Host "Instale o Git for Windows (https://git-scm.com/download/win), abra um PowerShell NOVO e rode de novo."
  exit 1
}

if (Test-Path "$dir\.git") {
  # sincroniza forcado com o remoto: resiste a historico reescrito (force-push)
  git -C $dir fetch origin --quiet
  git -C $dir reset --hard origin/main --quiet
} else { git clone $repo $dir }

Set-Location $dir
powershell -NoProfile -ExecutionPolicy Bypass -File "Instalar OmniGraph.ps1"
