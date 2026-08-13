#!/bin/bash
# =============================================================================
#  OmniGraph — instalação em um comando (macOS / Linux). Sem ZIP, sem Gatekeeper.
#
#  Uso (cole no Terminal):
#    curl -fsSL https://raw.githubusercontent.com/Zespek/OmniGraph/main/instalar.sh | bash
#
#  Ele clona o projeto em ~/OmniGraph (ou $OMNIGRAPH_DIR) e roda o instalador
#  completo (ferramenta + IA local + modelo). Rodar de novo apenas atualiza.
# =============================================================================
set -e
REPO="https://github.com/Zespek/OmniGraph.git"
DIR="${OMNIGRAPH_DIR:-$HOME/OmniGraph}"

echo "OmniGraph  ->  instalando em: $DIR"

if ! command -v git >/dev/null 2>&1; then
  echo "git não encontrado."
  case "$(uname -s)" in
    Darwin) echo "Rode 'xcode-select --install', aceite a instalação e tente de novo." ;;
    Linux)  echo "Instale o git (ex.: sudo apt install git) e tente de novo." ;;
  esac
  exit 1
fi

if [ -d "$DIR/.git" ]; then
  git -C "$DIR" pull --ff-only origin main
else
  git clone "$REPO" "$DIR"
fi

cd "$DIR"
bash "Instalar OmniGraph.command"
