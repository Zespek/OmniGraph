<div align="center">
  <img src="https://raw.githubusercontent.com/Zespek/Zespek/main/banner.webp" width="100%" alt="Header Zespek" style="border-radius: 8px;" />
</div>

<br/>

<div align="left">
  <img src="https://readme-typing-svg.herokuapp.com/?font=Fira+Code&weight=600&size=22&pause=2500&duration=2000&color=bd00ff&vCenter=true&width=800&height=40&lines=>_+./omnigraph.sh" alt="OmniGraph" />
</div>

```ini
zespek@server:~$ cat ./omnigraph.ini
[ Extrator Multimodal de Grafos de Conhecimento Local & Privativo ]

[O_Que_E]
  - "Ferramenta local que mapeia código, PDFs, imagens e mídias em um grafo interativo."
  - "Projetado para análise profunda de arquitetura de software e dependências."
  - "100% offline: privacidade absoluta para bases de código comerciais."

[Destaques]
  - "Extração estática via AST nativo para mais de 25 linguagens de programação."
  - "Agrupamento inteligente por comunidades usando o algoritmo de Leiden."
  - "Visualização interativa pronta em HTML para navegação direta."
```

---

<div align="left">
  <img src="https://readme-typing-svg.herokuapp.com/?font=Fira+Code&weight=600&size=22&pause=2500&duration=2000&color=bd00ff&vCenter=true&width=800&height=40&lines=>_+./abrir_o_guia.sh" alt="Abrir o Guia" />
</div>

> **Primeiro acesso? Comece pelo guia.** Passo a passo, simples e direto.

```ini
zespek@server:~$ ./abrir_o_guia.sh
[Abrir_o_Guia_de_Utilizacao]
  - "macOS  : dê um duplo-clique em   →  Abrir Guia.command"
  - "Outros : abra no navegador       →  guia de utilizacao/index.html"

[Instalar_com_UM_Comando]  # sem baixar ZIP, sem bloqueio do macOS
  - "macOS / Linux (no Terminal):"
  - "  curl -fsSL https://raw.githubusercontent.com/Zespek/OmniGraph/main/instalar.sh | bash"
  - ""
  - "Windows (no PowerShell):"
  - "  powershell -c \"irm https://raw.githubusercontent.com/Zespek/OmniGraph/main/instalar.ps1 | iex\""
```

> **Baixou o ZIP e o macOS travou com "a Apple não pôde verificar…"?** É a segurança do sistema com scripts baixados, não é erro do instalador. Prefira o comando acima (não usa ZIP). Se insistir no ZIP, no Terminal dentro da pasta: `xattr -dr com.apple.quarantine . && bash "Instalar OmniGraph.command"`

---

<div align="left">
  <img src="https://readme-typing-svg.herokuapp.com/?font=Fira+Code&weight=600&size=22&pause=2500&duration=2000&color=bd00ff&vCenter=true&width=800&height=40&lines=>_+./como_funciona.sh" alt="Como Funciona" />
</div>

```ini
zespek@server:~$ cat ./funcionalidades.ini
[Recursos_Principais]
  1. Analisador_Multimodal : "Código-fonte, imagens de diagramas, áudio/vídeo em um só lugar."
  2. AST_TreeSitter       : "Extração nativa e precisa sem dependência ou custos de APIs externas."
  3. Transcricao_Offline   : "Suporte a Whisper local para transcrição inteligente de documentação em áudio."
  4. Grafo_HTML            : "Visualizador interativo gerado em omnigraph-out/graph.html."
```

---

<div align="left">
  <img src="https://readme-typing-svg.herokuapp.com/?font=Fira+Code&weight=600&size=22&pause=2500&duration=2000&color=bd00ff&vCenter=true&width=800&height=40&lines=>_+./instalacao_e_uso.sh" alt="Instalação e Uso" />
</div>

```bash
# Recomendado: duplo-clique em "Instalar OmniGraph.command" (faz tudo sozinho)
# Ou, no terminal, a partir deste clone:
uv tool install --from . omnigraph && omnigraph install

# Executar a análise na pasta atual
omnigraph .

# O grafo interativo estará disponível localmente em:
# ./omnigraph-out/graph.html
```

---

<div align="left">
  <img src="https://readme-typing-svg.herokuapp.com/?font=Fira+Code&weight=600&size=22&pause=2500&duration=2000&color=bd00ff&vCenter=true&width=800&height=40&lines=>_+./creditos.sh" alt="Créditos" />
</div>

```ini
zespek@server:~$ cat ./creditos.ini
[Baseado_Em]
  - "OmniGraph é uma distribuição adaptada e localizada do projeto open-source Graphify."
  - "Licenciado sob a Apache License 2.0. Veja LICENSE e NOTICE."

[Mudancas_Nesta_Distribuicao]
  - "Rebranding para OmniGraph e tradução (comentários e docs) para pt-BR."
  - "Instalador multiplataforma (macOS, Linux, Windows) e IA local (Ollama)."
  - "Manutenção de dependências e segurança."
```

---

<div align="center">
  <p>Distribuição adaptada e mantida por <b><a href="https://github.com/Zespek">Zespek (José Felipe)</a></b> · baseada no projeto <b>Graphify</b> (Apache-2.0)</p>
</div>
