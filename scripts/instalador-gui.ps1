# =============================================================================
#  Instalador do OmniGraph com interface grafica (Windows).
#
#  Nao instala nada por conta propria: e uma casca em volta do
#  "Instalar OmniGraph.ps1" (a unica fonte da verdade da instalacao). Roda ele
#  com -Auto e vai mostrando a saida numa janela, para quem nao usa terminal.
#
#  Codigo de saida 2 = nao consegui abrir janela (Windows sem interface grafica);
#  o "Instalar OmniGraph.cmd" usa isso para cair no instalador de texto.
# =============================================================================
$ErrorActionPreference = "Stop"

try {
  Add-Type -AssemblyName System.Windows.Forms
  Add-Type -AssemblyName System.Drawing
} catch {
  Write-Host "Interface grafica indisponivel neste Windows."
  exit 2
}

$raiz = Split-Path $PSScriptRoot -Parent
$instalador = Join-Path $raiz "Instalar OmniGraph.ps1"
if (-not (Test-Path $instalador)) {
  [System.Windows.Forms.MessageBox]::Show(
    "Nao encontrei o 'Instalar OmniGraph.ps1' em:`n$raiz`n`nUse a pasta completa do OmniGraph.",
    "OmniGraph", "OK", "Error") | Out-Null
  exit 1
}

# quem baixou o ZIP recebe os arquivos "marcados como da internet"; sem isso o
# PowerShell recusa ate com Bypass em algumas politicas de empresa
Get-ChildItem $raiz -Recurse -Include *.ps1,*.cmd,*.py -ErrorAction SilentlyContinue |
  Unblock-File -ErrorAction SilentlyContinue

[System.Windows.Forms.Application]::EnableVisualStyles()

# ---------------------------------------------------------------- identidade --
$corFundo   = [System.Drawing.Color]::FromArgb(13, 7, 20)      # #0d0714
$corCartao  = [System.Drawing.Color]::FromArgb(22, 9, 31)      # #16091f
$corTerm    = [System.Drawing.Color]::FromArgb(10, 5, 16)      # #0a0510
$corRoxo    = [System.Drawing.Color]::FromArgb(189, 0, 255)    # #bd00ff
$corRoxoCl  = [System.Drawing.Color]::FromArgb(217, 77, 255)   # #d94dff
$corRoxoDk  = [System.Drawing.Color]::FromArgb(122, 47, 158)   # #7a2f9e
$corCiano   = [System.Drawing.Color]::FromArgb(0, 229, 255)    # #00e5ff
$corTexto   = [System.Drawing.Color]::FromArgb(237, 228, 245)  # #ede4f5
$corDim     = [System.Drawing.Color]::FromArgb(183, 159, 206)  # #b79fce
$corMute    = [System.Drawing.Color]::FromArgb(138, 111, 168)  # #8a6fa8
$corBorda   = [System.Drawing.Color]::FromArgb(51, 24, 74)     # #33184a

# Desenha o logo do OmniGraph (o mesmo grafo de docs/logo-icon.svg, viewBox 48x48):
# um hub central ligado a quatro satelites, um deles em ciano para o mapa nao
# ficar monocromatico.
function Novo-Logo([int]$px) {
  $bmp = New-Object System.Drawing.Bitmap($px, $px)
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $g.SmoothingMode = 'AntiAlias'
  $e = $px / 48.0   # escala do viewBox original
  $hub = @(24, 24)
  $sat = @(@(10, 11), @(39, 14), @(38, 36), @(11, 37))
  $canetaViva = New-Object System.Drawing.Pen($corRoxo, [float](1.2 * $e))
  $canetaFraca = New-Object System.Drawing.Pen($corRoxoDk, [float](1.1 * $e))
  foreach ($s in $sat) {
    $g.DrawLine($canetaViva, [float]($hub[0]*$e), [float]($hub[1]*$e), [float]($s[0]*$e), [float]($s[1]*$e))
  }
  for ($i = 0; $i -lt 4; $i++) {
    $a = $sat[$i]; $b = $sat[($i + 1) % 4]
    $g.DrawLine($canetaFraca, [float]($a[0]*$e), [float]($a[1]*$e), [float]($b[0]*$e), [float]($b[1]*$e))
  }
  function Bola($cx, $cy, $r, $borda, $nucleo, $rn) {
    $caneta = New-Object System.Drawing.Pen($borda, [float](1.3 * $e))
    $fundo = New-Object System.Drawing.SolidBrush($corFundo)
    $miolo = New-Object System.Drawing.SolidBrush($nucleo)
    $g.FillEllipse($fundo, [float](($cx-$r)*$e), [float](($cy-$r)*$e), [float](2*$r*$e), [float](2*$r*$e))
    $g.DrawEllipse($caneta, [float](($cx-$r)*$e), [float](($cy-$r)*$e), [float](2*$r*$e), [float](2*$r*$e))
    $g.FillEllipse($miolo, [float](($cx-$rn)*$e), [float](($cy-$rn)*$e), [float](2*$rn*$e), [float](2*$rn*$e))
  }
  Bola 10 11 2.6 $corRoxo $corRoxo 1.1
  Bola 39 14 2.6 $corRoxo $corRoxo 1.1
  Bola 38 36 2.8 $corCiano $corCiano 1.25   # o unico no ciano, como no logo
  Bola 11 37 2.6 $corRoxo $corRoxo 1.1
  Bola 24 24 4.4 $corRoxoCl $corRoxoCl 1.8  # hub central
  $g.Dispose()
  return $bmp
}

# --------------------------------------------------------------------- janela --
$L = 660   # largura util; a altura sai do tamanho real que a arte ocupar

$janela = New-Object System.Windows.Forms.Form
$janela.Text = "OmniGraph - instalacao"
$janela.FormBorderStyle = 'FixedDialog'
$janela.MaximizeBox = $false
$janela.StartPosition = 'CenterScreen'
$janela.BackColor = $corFundo
$janela.ForeColor = $corTexto
$janela.Font = New-Object System.Drawing.Font("Segoe UI", 9)
try { $janela.Icon = [System.Drawing.Icon]::FromHandle((Novo-Logo 64).GetHicon()) } catch {}

# cabecalho: a caveira roxa (a mesma arte do 'omnigraph install' e do instalador
# de terminal), o logo do grafo e o nome. Toda a arte e ASCII puro de proposito:
# o PowerShell 5.1 le .ps1 como ANSI e mastigaria caracteres especiais.
$cabecalho = New-Object System.Windows.Forms.Panel
$cabecalho.Location = New-Object System.Drawing.Point(0, 0)
$cabecalho.BackColor = $corCartao
$janela.Controls.Add($cabecalho)

# here-string de aspas SIMPLES: a caveira e cheia de crases, e entre aspas duplas
# o PowerShell as trataria como escape e comeria pedacos do desenho
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

# sem uma fonte monoespacada a arte ASCII desmonta; Courier New existe em
# qualquer Windows e serve de reserva
$fonteArte = "Courier New"
foreach ($f in @("Consolas", "Lucida Console")) {
  # FontFamily lanca excecao quando a fonte nao existe, e aqui o preference e Stop
  try { $familia = New-Object System.Drawing.FontFamily($f); $fonteArte = $familia.Name; break } catch {}
}

$arte = New-Object System.Windows.Forms.Label
$arte.Text = $caveira
$arte.Font = New-Object System.Drawing.Font($fonteArte, 7)
$arte.ForeColor = $corRoxo
$arte.BackColor = [System.Drawing.Color]::Transparent
$arte.AutoSize = $true
$cabecalho.Controls.Add($arte)
$arte.Location = New-Object System.Drawing.Point([int](($L - $arte.Width) / 2), 10)

# a partir daqui tudo se posiciona abaixo da arte: se a fonte render diferente
# em outra maquina, o layout acompanha em vez de sobrepor
$y = $arte.Bottom + 12

$logo = New-Object System.Windows.Forms.PictureBox
$logo.Image = Novo-Logo 56
$logo.Size = New-Object System.Drawing.Size(56, 56)
$logo.Location = New-Object System.Drawing.Point(26, $y)
$logo.BackColor = [System.Drawing.Color]::Transparent
$cabecalho.Controls.Add($logo)

$titulo = New-Object System.Windows.Forms.Label
$titulo.Text = "OmniGraph"
$titulo.Font = New-Object System.Drawing.Font("Segoe UI", 22, [System.Drawing.FontStyle]::Bold)
$titulo.ForeColor = $corRoxo
$titulo.AutoSize = $true
$titulo.Location = New-Object System.Drawing.Point(96, $y)
$cabecalho.Controls.Add($titulo)

$subtitulo = New-Object System.Windows.Forms.Label
$subtitulo.Text = "O mapa do seu projeto. Roda no seu computador, sem gastar tokens de API."
$subtitulo.ForeColor = $corDim
$subtitulo.AutoSize = $true
$subtitulo.Location = New-Object System.Drawing.Point(100, ($y + 42))
$cabecalho.Controls.Add($subtitulo)

$cabecalho.Size = New-Object System.Drawing.Size($L, ($y + 70))

$risco = New-Object System.Windows.Forms.Panel
$risco.Location = New-Object System.Drawing.Point(0, $cabecalho.Bottom)
$risco.Size = New-Object System.Drawing.Size($L, 2)
$janela.Controls.Add($risco)
$risco.BackColor = $corRoxo

$y = $risco.Bottom + 18

# opcao: instalacao leve (sem baixar GBs de modelo)
$opcaoIA = New-Object System.Windows.Forms.CheckBox
$opcaoIA.Text = "Instalar a IA local (responde perguntas em portugues; baixa alguns GB)"
$opcaoIA.Checked = $true
$opcaoIA.ForeColor = $corTexto
$opcaoIA.AutoSize = $true
$opcaoIA.Location = New-Object System.Drawing.Point(26, $y)
$janela.Controls.Add($opcaoIA)

# situacao + barra de progresso desenhada a mao (a nativa nao aceita a cor roxa)
$situacao = New-Object System.Windows.Forms.Label
$situacao.Text = "Pronto para instalar."
$situacao.ForeColor = $corDim
$situacao.AutoSize = $false
$situacao.Size = New-Object System.Drawing.Size(($L - 52), 20)
$situacao.Location = New-Object System.Drawing.Point(26, ($y + 32))
$janela.Controls.Add($situacao)

$trilho = New-Object System.Windows.Forms.Panel
$trilho.Location = New-Object System.Drawing.Point(26, ($y + 56))
$trilho.Size = New-Object System.Drawing.Size(($L - 52), 8)
$trilho.BackColor = $corBorda
$janela.Controls.Add($trilho)

$barra = New-Object System.Windows.Forms.Panel
$barra.Location = New-Object System.Drawing.Point(0, 0)
$barra.Size = New-Object System.Drawing.Size(0, 8)
$barra.BackColor = $corRoxo
$trilho.Controls.Add($barra)

# log (o que o instalador esta fazendo)
$log = New-Object System.Windows.Forms.TextBox
$log.Multiline = $true
$log.ReadOnly = $true
$log.ScrollBars = 'Vertical'
$log.BackColor = $corTerm
$log.ForeColor = $corDim
$log.BorderStyle = 'FixedSingle'
$log.Font = New-Object System.Drawing.Font($fonteArte, 9)
$log.Location = New-Object System.Drawing.Point(26, ($y + 78))
$log.Size = New-Object System.Drawing.Size(($L - 52), 190)
$janela.Controls.Add($log)

$dica = New-Object System.Windows.Forms.Label
$dica.Text = "Instala so para o seu usuario. Nao precisa de administrador."
$dica.ForeColor = $corMute
$dica.AutoSize = $true
$dica.Location = New-Object System.Drawing.Point(28, ($log.Bottom + 8))
$janela.Controls.Add($dica)

$yBotoes = $log.Bottom + 32

function Novo-Botao($texto, $x, $largura, $principal) {
  $b = New-Object System.Windows.Forms.Button
  $b.Text = $texto
  $b.Location = New-Object System.Drawing.Point($x, $yBotoes)
  $b.Size = New-Object System.Drawing.Size($largura, 36)
  $b.FlatStyle = 'Flat'
  $b.FlatAppearance.BorderSize = 1
  if ($principal) {
    $b.BackColor = $corRoxo
    $b.ForeColor = $corFundo
    $b.Font = New-Object System.Drawing.Font("Segoe UI", 10, [System.Drawing.FontStyle]::Bold)
    $b.FlatAppearance.BorderColor = $corRoxo
  } else {
    $b.BackColor = $corCartao
    $b.ForeColor = $corDim
    $b.FlatAppearance.BorderColor = $corBorda
  }
  return $b
}

$btInstalar = Novo-Botao "Instalar" 26 190 $true
$btIde      = Novo-Botao "Usar no meu projeto (IDE)" 228 230 $false
$btFechar   = Novo-Botao "Fechar" ($L - 126) 100 $false
$btIde.Enabled = $false
$janela.Controls.AddRange(@($btInstalar, $btIde, $btFechar))

$janela.ClientSize = New-Object System.Drawing.Size($L, ($yBotoes + 58))

# ------------------------------------------------------------------- execucao --
$estado = [ordered]@{
  processo   = $null
  arquivoLog = $null
  lidas      = 0
  aoTerminar = $null
  pct        = 0
}

function Escrever($texto) {
  $log.AppendText($texto + "`r`n")
  $log.SelectionStart = $log.TextLength
  $log.ScrollToCaret()
}

function Progresso([int]$pct, $texto) {
  if ($pct -gt $estado.pct) { $estado.pct = $pct }   # nunca anda para tras
  $barra.Width = [int]($trilho.Width * $estado.pct / 100)
  if ($texto) { $situacao.Text = $texto }
}

# le so as linhas novas do log; FileShare ReadWrite para nao brigar com quem escreve
function Ler-Novas {
  if (-not $estado.arquivoLog -or -not (Test-Path $estado.arquivoLog)) { return @() }
  try {
    $fs = [System.IO.File]::Open($estado.arquivoLog, 'Open', 'Read', 'ReadWrite')
    $sr = New-Object System.IO.StreamReader($fs, [System.Text.Encoding]::Default)
    $todo = $sr.ReadToEnd()
    $sr.Dispose(); $fs.Dispose()
  } catch { return @() }
  $linhas = $todo -split "`r?`n"
  # a ultima linha pode estar pela metade; so consome quando o arquivo termina em quebra
  if ($todo -notmatch "`n$" -and $linhas.Count -gt 0) {
    $linhas = $linhas[0..($linhas.Count - 2)]
  }
  if ($linhas.Count -le $estado.lidas) { return @() }
  $novas = $linhas[$estado.lidas..($linhas.Count - 1)]
  $estado.lidas = $linhas.Count
  return $novas
}

# traduz o que o instalador imprime em % e numa frase curta
$etapas = [ordered]@{
  "Preparando o ambiente"        = @(10, "Preparando o ambiente...")
  "Verificando atualizacoes"     = @(20, "Verificando atualizacoes...")
  "Instalando o OmniGraph"       = @(35, "Instalando a ferramenta...")
  "Configurando a IA local"      = @(55, "Instalando a IA local (pode demorar)...")
  "IA local"                     = @(80, "Instalacao leve: sem IA local.")
  "Ajustando o terminal"         = @(88, "Ajustando o terminal...")
  "Verificando a instalacao"     = @(95, "Conferindo se ficou tudo certo...")
}

# cores ANSI do ollama viram lixo na tela; `e so existe no PowerShell 7, e o
# Windows entrega o 5.1 - por isso o escape vem de [char]27
$ansi = ([char]27) + "\[[0-9;]*[a-zA-Z]"

$relogio = New-Object System.Windows.Forms.Timer
$relogio.Interval = 300
$relogio.Add_Tick({
  foreach ($linha in Ler-Novas) {
    $limpa = ($linha -replace $ansi, "").TrimEnd()
    if ($limpa -eq "") { continue }
    Escrever $limpa
    foreach ($chave in $etapas.Keys) {
      if ($limpa -like "*$chave*") { Progresso $etapas[$chave][0] $etapas[$chave][1] }
    }
    # o download do modelo e o passo longo: mostra a % do proprio ollama
    if ($limpa -match "pulling.*?(\d+)\s*%") {
      Progresso 55 ("Baixando o modelo da IA... " + $Matches[1] + "%")
    }
  }
  if ($estado.processo -and $estado.processo.HasExited) {
    $relogio.Stop()
    Start-Sleep -Milliseconds 200
    foreach ($linha in Ler-Novas) {
      $limpa = ($linha -replace $ansi, "").TrimEnd()
      if ($limpa -ne "") { Escrever $limpa }
    }
    $codigo = $estado.processo.ExitCode
    $estado.processo = $null
    if ($estado.aoTerminar) { & $estado.aoTerminar $codigo }
  }
})

function Rodar($arquivoPs1, $argumentos, $aoTerminar) {
  $estado.arquivoLog = Join-Path $env:TEMP ("omnigraph-instalador-" + $PID + ".log")
  $erroLog = $estado.arquivoLog + ".err"
  Remove-Item $estado.arquivoLog, $erroLog -Force -ErrorAction SilentlyContinue
  $estado.lidas = 0
  $estado.aoTerminar = $aoTerminar
  # o Start-Process junta a lista com espaco e NAO poe aspas sozinho; sem isso,
  # "Instalar OmniGraph.ps1" (e pastas com espaco) chegariam partidos ao lado
  $citar = { param($t) if ($t -match '\s') { '"' + $t + '"' } else { $t } }
  $lista = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (& $citar $arquivoPs1))
  foreach ($a in $argumentos) { $lista += (& $citar $a) }
  $estado.processo = Start-Process powershell -ArgumentList $lista -WindowStyle Hidden -PassThru `
      -RedirectStandardOutput $estado.arquivoLog -RedirectStandardError $erroLog
  $relogio.Start()
}

$btInstalar.Add_Click({
  $btInstalar.Enabled = $false
  $btIde.Enabled = $false
  $opcaoIA.Enabled = $false
  $log.Clear()
  $estado.pct = 0
  Progresso 3 "Instalando..."
  $extras = @('-Auto')
  if (-not $opcaoIA.Checked) { $extras += '-SemIA' }
  Rodar $instalador $extras {
    param($codigo)
    $btInstalar.Enabled = $true
    $opcaoIA.Enabled = $true
    if ($codigo -eq 0) {
      Progresso 100 "Tudo pronto! Agora aponte para o seu projeto."
      $btInstalar.Text = "Instalar de novo"
      $btIde.Enabled = $true
      # o instalador acabou de mexer no PATH do usuario; recarrega para o botao
      # da IDE achar o 'omnigraph' sem precisar reabrir a janela
      $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                  [Environment]::GetEnvironmentVariable("Path", "User")
    } else {
      $situacao.Text = "A instalacao nao terminou. Leia as ultimas linhas acima."
      $situacao.ForeColor = $corRoxoCl
    }
  }
})

$btIde.Add_Click({
  $seletor = New-Object System.Windows.Forms.FolderBrowserDialog
  $seletor.Description = "Escolha a pasta do projeto que voce quer mapear"
  if ($seletor.ShowDialog() -ne 'OK') { return }
  $projeto = $seletor.SelectedPath
  $ide = @(
    (Join-Path "$env:LOCALAPPDATA\OmniGraph\lib" "omnigraph-ide.ps1"),
    (Join-Path $PSScriptRoot "omnigraph-ide.ps1")
  ) | Where-Object { Test-Path $_ } | Select-Object -First 1
  if (-not $ide) {
    [System.Windows.Forms.MessageBox]::Show("Instale o OmniGraph primeiro.", "OmniGraph") | Out-Null
    return
  }
  $btIde.Enabled = $false
  $btInstalar.Enabled = $false
  $estado.pct = 0
  $log.Clear()
  Progresso 5 "Mapeando $projeto ..."
  Rodar $ide @($projeto) {
    param($codigo)
    $btIde.Enabled = $true
    $btInstalar.Enabled = $true
    if ($codigo -eq 0) {
      Progresso 100 "Projeto pronto. Pergunte sobre ele na sua IDE, ou digite /omnigraph."
    } else {
      $situacao.Text = "Nao consegui preparar esse projeto. Leia as linhas acima."
      $situacao.ForeColor = $corRoxoCl
    }
  }
})

$btFechar.Add_Click({ $janela.Close() })

# fechar no meio da instalacao deixaria o processo orfao baixando GBs em silencio
$janela.Add_FormClosing({
  param($remetente, $evento)
  if ($estado.processo -and -not $estado.processo.HasExited) {
    $r = [System.Windows.Forms.MessageBox]::Show(
      "A instalacao ainda esta rodando. Cancelar mesmo assim?",
      "OmniGraph", "YesNo", "Warning")
    if ($r -ne "Yes") { $evento.Cancel = $true; return }
    try { $estado.processo.Kill() } catch {}
  }
  $relogio.Stop()
})

Escrever "OmniGraph - instalador para Windows"
Escrever "Clique em 'Instalar'. Da para rodar quantas vezes quiser: se ja estiver"
Escrever "instalado, ele so atualiza o que mudou."
Escrever ""

[void]$janela.ShowDialog()
