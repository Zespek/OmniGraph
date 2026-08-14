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

# se a IA estiver parada mas instalada, sobe sozinha
function IaDisponivel {
  $h = if ($env:OLLAMA_HOST) { $env:OLLAMA_HOST } else { "localhost:11434" }
  try { Invoke-RestMethod "http://$h/api/tags" -TimeoutSec 3 | Out-Null; return $true } catch { return $false }
}
if (-not (IaDisponivel) -and (Get-Command ollama -ErrorAction SilentlyContinue)) {
  Write-Host "> iniciando a IA local (Ollama), aguarde..." -ForegroundColor Magenta
  Start-Process ollama -ArgumentList "serve" -WindowStyle Hidden -ErrorAction SilentlyContinue
  for ($i=0; $i -lt 30; $i++) { if (IaDisponivel) { break }; Start-Sleep 1 }
}
if (-not (IaDisponivel)) {
  Write-Host "A IA local nao esta ativa - a resposta pode ficar limitada." -ForegroundColor Yellow
}

Write-Host "> Pergunta: $pergunta" -ForegroundColor Magenta

# 1) recupera do mapa o trecho relevante (contexto)
Push-Location $alvo
try { $ctx = (& $og query $pergunta 2>$null | Out-String) } finally { Pop-Location }

if (-not $ctx -or $ctx -match "No matching nodes") {
  Write-Host "Nao encontrei nada relacionado a isso no mapa." -ForegroundColor Yellow
  Write-Host "Tente outras palavras - um nome de funcao, arquivo ou tela do projeto."
  exit 0
}

# 2) a IA local redige a resposta em portugues a partir do contexto
$modelo = if ($env:OLLAMA_MODEL) { $env:OLLAMA_MODEL } else { "qwen2.5-coder:7b" }
$host2  = if ($env:OLLAMA_HOST) { $env:OLLAMA_HOST } else { "localhost:11434" }
$temModelo = $false
try { $tags = Invoke-RestMethod "http://$host2/api/tags" -TimeoutSec 3
      if ($tags.models.name -match [regex]::Escape(($modelo -split ':')[0])) { $temModelo = $true } } catch {}

if ((IaDisponivel) -and $temModelo) {
  $prompt = @"
Voce e um assistente que explica projetos de software em portugues claro e simples, para uma pessoa nao tecnica. Responda a PERGUNTA usando SOMENTE o CONTEXTO abaixo (um trecho do mapa do projeto: NODE = uma parte do codigo, EDGE = uma ligacao entre partes; "calls" = uma funcao chama outra, "contains" = um arquivo contem algo). Seja direto, de 2 a 5 frases, sem jargao. Se o contexto nao bastar, diga o que da para afirmar.

PERGUNTA: $pergunta

CONTEXTO:
$ctx

RESPOSTA (em portugues):
"@
  Write-Progress -Activity "Perguntando a IA local" -Status "a IA esta pensando..."
  try {
    $body = @{ model = $modelo; prompt = $prompt; stream = $false } | ConvertTo-Json
    $r = Invoke-RestMethod "http://$host2/api/generate" -Method Post -ContentType 'application/json' -Body $body -TimeoutSec 180
    Write-Progress -Activity "Perguntando a IA local" -Completed
    if ($r.response) {
      Write-Host "> Resposta:" -ForegroundColor Green
      Write-Host $r.response.Trim()
    } else {
      Write-Host "A IA nao redigiu a resposta - partes do mapa relacionadas:" -ForegroundColor Yellow
      Write-Host $ctx
    }
  } catch {
    Write-Progress -Activity "Perguntando a IA local" -Completed
    Write-Host "A IA nao respondeu - partes do mapa relacionadas:" -ForegroundColor Yellow
    Write-Host $ctx
  }
} else {
  Write-Host "A IA local nao esta ativa - mostrando as partes do mapa relacionadas:" -ForegroundColor Yellow
  Write-Host $ctx
  Write-Host "Para respostas em portugues, ative a IA (instale o Ollama e rode o instalador de novo)."
}
