#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
#  Instalador do OmniGraph — macOS e Linux.
#
#  macOS: duplo-clique no Finder. Se a Apple bloquear ("não foi possível
#         verificar..."), é o Gatekeeper com arquivo baixado. Rode pelo Terminal:
#             bash "Instalar OmniGraph.command"
#         (rodar por dentro do Terminal pula o bloqueio).
#  Linux: rode no terminal com:  bash "Instalar OmniGraph.command"
#
#  Faz tudo sozinho: instala a ferramenta, verifica atualizações, instala a IA
#  local (Ollama) e baixa o modelo. Pode rodar de novo quando quiser.
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail
cd "$(dirname "$0")" || exit 1

SO="$(uname -s)"   # Darwin (macOS) ou Linux
roxo=$'\033[1;35m'; verde=$'\033[32m'; amarelo=$'\033[33m'; zero=$'\033[0m'
titulo(){ printf "\n${roxo}▸ %s${zero}\n" "$1"; }
ok(){ printf "  ${verde}✓${zero} %s\n" "$1"; }
aviso(){ printf "  ${amarelo}!${zero} %s\n" "$1"; }
abrir(){ case "$SO" in Darwin) open "$1";; Linux) xdg-open "$1";; esac >/dev/null 2>&1 || true; }

echo "════════════════════════════════════════════════"
echo "   OmniGraph — instalação e atualização ($SO)"
echo "════════════════════════════════════════════════"

# 1) uv (gerenciador que roda tudo) ────────────────────────────────────────────
titulo "Preparando o ambiente"
export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  aviso "instalando o uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
command -v uv >/dev/null 2>&1 && ok "uv pronto" || aviso "não consegui instalar o uv"

# 2) verificar atualizações ────────────────────────────────────────────────────
titulo "Verificando atualizações"
if command -v git >/dev/null 2>&1 && [ -d .git ]; then
  if git fetch origin --quiet 2>/dev/null; then
    # local == remoto?  (compara os commits)
    if [ "$(git rev-parse HEAD 2>/dev/null)" = "$(git rev-parse origin/main 2>/dev/null)" ]; then
      ok "já está na última versão"
    else
      aviso "atualização disponível"
      printf "  Atualizar agora? [S/n] "; read -r r
      case "${r:-S}" in
        [Nn]*) aviso "pulando a atualização";;
        # reset --hard resiste a histórico reescrito (force-push) sem quebrar
        *) git reset --hard origin/main --quiet && ok "atualizado para a última versão";;
      esac
    fi
  else
    aviso "sem internet para verificar (seguindo assim mesmo)"
  fi
fi

# 3) instalar a ferramenta ─────────────────────────────────────────────────────
titulo "Instalando o OmniGraph"
# pasta onde o uv coloca o executavel (fonte da verdade; cai pra ~/.local/bin)
BIN="$(uv tool dir --bin 2>/dev/null)"; [ -z "$BIN" ] && BIN="$HOME/.local/bin"
export PATH="$BIN:$PATH"
# [ollama] traz o pacote 'openai' que o backend local exige; os demais habilitam
# PDFs, docs, .sql, watch e a integração com assistentes (mcp). Todos têm wheel.
inst_log="$(uv tool install --from ".[ollama,mcp,pdf,office,watch,sql]" omnigraph --force 2>&1)"
uv tool update-shell >/dev/null 2>&1 || true
if [ -x "$BIN/omnigraph" ]; then
  ok "comandos 'omnigraph' e 'omnigraph-mcp' instalados"
else
  aviso "FALHA ao instalar a ferramenta. Detalhe do erro:"
  printf '%s\n' "$inst_log" | tail -12 | sed 's/^/      /'
  aviso "Copie essas linhas e mande para o suporte."
fi
# comando amigável com barra de progresso
if [ -f scripts/omnigraph-mapa ]; then
  cp scripts/omnigraph-mapa "$BIN/omnigraph-mapa" 2>/dev/null && chmod +x "$BIN/omnigraph-mapa" 2>/dev/null \
    && ok "comando 'omnigraph-mapa' instalado (gera o mapa mostrando o progresso)"
fi

# 4) IA local (Ollama) — sem gastar tokens de API ─────────────────────────────
titulo "Configurando a IA local (Ollama)"
if ! command -v ollama >/dev/null 2>&1; then
  case "$SO" in
    Linux)
      aviso "instalando o Ollama..."
      curl -fsSL https://ollama.com/install.sh | sh
      ;;
    Darwin)
      if command -v brew >/dev/null 2>&1; then
        aviso "instalando o Ollama via Homebrew..."
        brew install ollama
      else
        aviso "Ollama não encontrado. Abrindo a página de download..."
        aviso "Instale o Ollama e rode este instalador de novo."
        abrir "https://ollama.com/download"
      fi
      ;;
  esac
fi
ia_ok=0
if command -v ollama >/dev/null 2>&1; then
  ok "Ollama instalado"
  # sobe o Ollama como SERVIÇO PERSISTENTE (sobe sozinho no login) — antes ele
  # morria ao fechar o instalador, e aí o extract falhava por servidor desligado.
  case "$SO" in
    Darwin)
      if command -v brew >/dev/null 2>&1 && brew services start ollama >/dev/null 2>&1; then
        ok "Ollama configurado para iniciar sozinho (brew services)"
      else
        (ollama serve >/dev/null 2>&1 &)   # reserva
      fi ;;
    Linux)
      if command -v systemctl >/dev/null 2>&1 && systemctl enable --now ollama >/dev/null 2>&1; then
        ok "Ollama configurado como serviço (systemd)"
      else
        (ollama serve >/dev/null 2>&1 &)   # reserva
      fi ;;
  esac
  # espera o servidor responder (até ~20s)
  for _ in $(seq 1 20); do curl -s localhost:11434/api/tags >/dev/null 2>&1 && break; sleep 1; done

  # modelo
  if curl -s localhost:11434/api/tags 2>/dev/null | grep -q "qwen2.5-coder:7b"; then
    ok "modelo qwen2.5-coder:7b já baixado"
  else
    aviso "baixando o modelo qwen2.5-coder:7b (~4.7GB — pode demorar)..."
    ollama pull qwen2.5-coder:7b && ok "modelo pronto" || aviso "falha ao baixar o modelo"
  fi

  # TESTE REAL de ponta a ponta: um mini-extract com Ollama num doc de exemplo.
  # (passa pelo mesmo caminho do 'openai' que faltava — pega o problema de verdade.)
  aviso "testando a IA local de verdade (pode levar ~30s na 1ª vez)..."
  smoke_dir="$(mktemp -d 2>/dev/null || echo "$HOME/.omnigraph_smoke")"
  mkdir -p "$smoke_dir"
  export OLLAMA_HOST=localhost:11434 OLLAMA_API_KEY=ollama
  printf '# Modulo de Pagamento\n\nEste modulo processa pagamentos e conversa com o Banco de Dados.\n' > "$smoke_dir/exemplo.md"
  if "$BIN/omnigraph" extract "$smoke_dir" --backend ollama >/dev/null 2>&1; then
    ok "IA local respondeu — pronta para uso"
    ia_ok=1
  else
    aviso "a IA local NÃO respondeu ao teste (o mapa ainda funciona sem ela)."
    aviso "Use o modo sem IA:  omnigraph extract . --code-only"
  fi
  rm -rf "$smoke_dir"
fi

# 5) persistir PATH + IA local (funciona em zsh E bash) ─────────────────────────
titulo "Ajustando o terminal (PATH e IA local)"
# um arquivo unico com o ambiente, carregado por qualquer shell
env_file="$HOME/.omnigraph_env"
{
  echo "# OmniGraph — ambiente (gerado pelo instalador). Nao edite."
  echo "export PATH=\"$BIN:\$PATH\""
  echo "export OLLAMA_HOST=localhost:11434"
  echo "export OLLAMA_API_KEY=ollama   # valor qualquer: apenas silencia um aviso"
} > "$env_file"
# faz cada perfil de shell carregar esse arquivo (sem duplicar)
linha_src='[ -f "$HOME/.omnigraph_env" ] && . "$HOME/.omnigraph_env"  # OmniGraph'
for perfil in "$HOME/.zshrc" "$HOME/.zprofile" "$HOME/.bashrc" "$HOME/.bash_profile" "$HOME/.profile"; do
  [ -e "$perfil" ] || { case "$perfil" in */.zshrc|*/.bash_profile|*/.profile) : ;; *) continue ;; esac; }
  grep -q '.omnigraph_env' "$perfil" 2>/dev/null || printf '\n%s\n' "$linha_src" >> "$perfil"
done
ok "PATH e IA local configurados (zsh e bash)"

# 6) verificação final ─────────────────────────────────────────────────────────
titulo "Verificando a instalação"
if "$BIN/omnigraph" --version >/dev/null 2>&1; then
  ok "OK! omnigraph responde: $("$BIN/omnigraph" --version 2>/dev/null | head -1)"
  instalou_ok=1
else
  aviso "o comando 'omnigraph' ainda não respondeu (veja os erros acima)"
  instalou_ok=0
fi

echo ""
echo "════════════════════════════════════════════════"
if [ "${instalou_ok:-0}" = 1 ]; then ok "Tudo pronto!"; else aviso "Instalação incompleta — leia as mensagens acima."; fi
echo ""
echo "  O comando 'omnigraph' funciona em QUALQUER pasta —"
echo "  você NÃO precisa estar dentro da pasta do OmniGraph."
echo ""
aviso "Para usar, ative o comando de uma destas formas:"
echo "    • feche este terminal e abra um NOVO   (mais simples), ou"
echo "    • rode agora, neste mesmo terminal:   source \"\$HOME/.omnigraph_env\""
echo ""
echo "  Depois, entre no SEU projeto e gere o mapa com UM comando:"
echo "      cd /caminho/do/seu/projeto"
if [ "${ia_ok:-0}" = 1 ]; then
  echo "      omnigraph-mapa            (com IA local, mostra a % de progresso)"
else
  echo "      omnigraph-mapa --code-only   (sem IA — a IA não passou no teste desta vez)"
fi
echo "  Ele mostra a barra de progresso e abre o gráfico (omnigraph-out/graph.html) sozinho."
echo ""
echo "  Instruções completas: abra 'guia de utilizacao/index.html'"
echo "════════════════════════════════════════════════"
echo ""
printf "Pressione Enter para fechar. "; read -r _
