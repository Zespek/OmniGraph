#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
#  Instalador do OmniGraph — para usar, dê um duplo-clique no Finder.
#  Faz tudo sozinho: instala a ferramenta, verifica atualizações, instala a IA
#  local (Ollama) e baixa o modelo. Pode rodar de novo quando quiser — ele só
#  atualiza o que faltar.
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail
cd "$(dirname "$0")" || exit 1

roxo=$'\033[1;35m'; verde=$'\033[32m'; amarelo=$'\033[33m'; zero=$'\033[0m'
titulo(){ printf "\n${roxo}▸ %s${zero}\n" "$1"; }
ok(){ printf "  ${verde}✓${zero} %s\n" "$1"; }
aviso(){ printf "  ${amarelo}!${zero} %s\n" "$1"; }

echo "════════════════════════════════════════════════"
echo "   OmniGraph — instalação e atualização"
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
    atras=$(git rev-list HEAD..origin/main --count 2>/dev/null || echo 0)
    if [ "${atras:-0}" -gt 0 ]; then
      aviso "há ${atras} atualização(ões) disponível(is)"
      printf "  Atualizar agora? [S/n] "; read -r r
      case "${r:-S}" in
        [Nn]*) aviso "pulando a atualização";;
        *) git pull --ff-only origin main && ok "atualizado para a última versão";;
      esac
    else
      ok "já está na última versão"
    fi
  else
    aviso "sem internet para verificar (seguindo assim mesmo)"
  fi
fi

# 3) instalar a ferramenta ─────────────────────────────────────────────────────
titulo "Instalando o OmniGraph"
if uv tool install --from . omnigraph --force >/dev/null 2>&1; then
  ok "comandos 'omnigraph' e 'omnigraph-mcp' instalados"
else
  aviso "falha ao instalar a ferramenta"
fi

# 4) IA local (Ollama) — sem gastar tokens de API ─────────────────────────────
titulo "Configurando a IA local (Ollama)"
if ! command -v ollama >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then
    aviso "instalando o Ollama via Homebrew..."
    brew install ollama
  else
    aviso "Ollama não encontrado. Abrindo a página de download..."
    aviso "Instale o Ollama e rode este instalador de novo."
    open "https://ollama.com/download" 2>/dev/null || true
  fi
fi
if command -v ollama >/dev/null 2>&1; then
  ok "Ollama instalado"
  # garante o servidor no ar
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

# 5) usar a IA local por padrão ────────────────────────────────────────────────
titulo "Definindo a IA local como padrão"
perfil="$HOME/.zshrc"
if grep -q "OLLAMA_HOST" "$perfil" 2>/dev/null; then
  ok "IA local já era o padrão"
else
  {
    echo ""
    echo "# OmniGraph: usar a IA local (Ollama) por padrão, sem gastar tokens de API"
    echo "export OLLAMA_HOST=localhost:11434"
  } >> "$perfil"
  ok "IA local definida como padrão"
fi

echo ""
echo "════════════════════════════════════════════════"
ok "Tudo pronto!"
echo "   • Abra um projeto no terminal e rode:  omnigraph extract ."
echo "   • Instruções: duplo-clique em 'Abrir Guia.command'"
echo "════════════════════════════════════════════════"
echo ""
printf "Pressione Enter para fechar. "; read -r _
