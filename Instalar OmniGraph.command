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
if uv tool install --from . omnigraph --force >/dev/null 2>&1; then
  ok "comandos 'omnigraph' e 'omnigraph-mcp' instalados"
  # garante que a pasta dos comandos (~/.local/bin) fique no PATH de terminais futuros
  uv tool update-shell >/dev/null 2>&1 || true
else
  aviso "falha ao instalar a ferramenta"
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
if command -v ollama >/dev/null 2>&1; then
  ok "Ollama instalado"
  if ! curl -s localhost:11434/api/tags >/dev/null 2>&1; then
    aviso "iniciando o serviço do Ollama..."
    (ollama serve >/dev/null 2>&1 &) ; sleep 3
  fi
  if curl -s localhost:11434/api/tags 2>/dev/null | grep -q "qwen2.5-coder:7b"; then
    ok "modelo qwen2.5-coder:7b já baixado"
  else
    aviso "baixando o modelo qwen2.5-coder:7b (~4.7GB — pode demorar)..."
    ollama pull qwen2.5-coder:7b && ok "modelo pronto" || aviso "falha ao baixar o modelo"
  fi
fi

# 5) persistir PATH + IA local no perfil ───────────────────────────────────────
titulo "Ajustando o terminal (PATH e IA local)"
case "$SO" in
  Darwin) perfil="$HOME/.zshrc" ;;
  Linux)  perfil="${SHELL##*/}" ; [ "$perfil" = "zsh" ] && perfil="$HOME/.zshrc" || perfil="$HOME/.bashrc" ;;
  *)      perfil="$HOME/.profile" ;;
esac
# garante que a pasta dos comandos esteja no PATH ao abrir um terminal novo
if grep -q '.local/bin' "$perfil" 2>/dev/null; then
  ok "PATH já configurado"
else
  {
    echo ""
    echo "# OmniGraph: deixar os comandos (omnigraph) acessíveis no terminal"
    echo 'export PATH="$HOME/.local/bin:$PATH"'
  } >> "$perfil"
  ok "PATH configurado (comando 'omnigraph' disponível em terminais novos)"
fi
if grep -q "OLLAMA_HOST" "$perfil" 2>/dev/null; then
  ok "IA local já era o padrão"
else
  {
    echo "# OmniGraph: usar a IA local (Ollama) por padrão, sem gastar tokens de API"
    echo "export OLLAMA_HOST=localhost:11434"
  } >> "$perfil"
  ok "IA local definida como padrão"
fi

echo ""
echo "════════════════════════════════════════════════"
ok "Tudo pronto!"
echo ""
aviso "IMPORTANTE: feche este terminal e abra um NOVO antes de usar o comando."
echo "   (é o que carrega o 'omnigraph' no PATH — sem isso dá 'command not found')."
echo ""
echo "   No terminal novo, dentro do seu projeto, rode:  omnigraph extract ."
echo "   Instruções: abra 'guia de utilizacao/index.html'"
echo "════════════════════════════════════════════════"
echo ""
printf "Pressione Enter para fechar. "; read -r _
