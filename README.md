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

[Windows]  # sem terminal: duplo-clique e pronto
  - "1. Code -> Download ZIP  e extraia"
  - "2. Duplo-clique em  ->  Instalar OmniGraph.cmd"
  - "3. Na janela do OmniGraph, clique em 'Instalar'."
  - ""
  - "Esse arquivo tambem funciona sozinho: se voce salvar so ele,"
  - "ele baixa o projeto e instala do mesmo jeito."

[Instalar_com_UM_Comando]  # sem baixar ZIP, sem bloqueio do macOS
  - "macOS / Linux (no Terminal):"
  - "  curl -fsSL https://raw.githubusercontent.com/Zespek/OmniGraph/main/instalar.sh | bash"
  - ""
  - "Windows (no PowerShell):"
  - "  powershell -c \"irm https://raw.githubusercontent.com/Zespek/OmniGraph/main/instalar.ps1 | iex\""
```

> **Baixou o ZIP e o macOS travou com "a Apple não pôde verificar…"?** É a segurança do sistema com scripts baixados, não é erro do instalador. Prefira o comando acima (não usa ZIP). Se insistir no ZIP, no Terminal dentro da pasta: `xattr -dr com.apple.quarantine . && bash "Instalar OmniGraph.command"`
>
> **No Windows, `.ps1` é bloqueado por padrão** ("a execução de scripts foi desabilitada neste sistema"). Por isso o ponto de entrada é o **`Instalar OmniGraph.cmd`**, e os comandos (`omnigraph-mapa`, `omnigraph-atualizar`…) são `.cmd` no PATH. Você **não** precisa rodar `Set-ExecutionPolicy` nem ser administrador.
>
> **Apareceu "O Windows protegeu o seu computador" (SmartScreen/Defender)?** É o padrão para qualquer arquivo baixado que não seja assinado digitalmente — não é vírus nem erro. Clique em **"Mais informações" → "Executar assim mesmo"**. Se preferir não ver esse aviso, use a linha única abaixo: ela não baixa arquivo nenhum para o disco.
>
> **Instalou uma versão antiga e o `omnigraph-atualizar` deu esse erro?** Ele não consegue se consertar sozinho (o próprio atualizador está bloqueado). Rode a linha única de instalação **uma vez** — ela não é um arquivo `.ps1`, então passa por qualquer política, e já reinstala os comandos no formato novo:
>
> ```powershell
> powershell -c "irm https://raw.githubusercontent.com/Zespek/OmniGraph/main/instalar.ps1 | iex"
> ```

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
# Recomendado: duplo-clique no instalador (faz tudo sozinho)
#   macOS / Linux : "Instalar OmniGraph.command"
#   Windows       : "Instalar OmniGraph.cmd"   (abre a janela com o botão Instalar)
# Ou, no terminal, a partir deste clone (os extras habilitam a IA local, PDFs, docs):
uv tool install --from ".[ollama,mcp,pdf,office,watch,sql]" omnigraph --force && omnigraph install

# Gerar o mapa na pasta atual (mostra a % de progresso e abre o gráfico):
omnigraph-mapa

# Sem IA (mais rápido, sempre funciona):
#   omnigraph-mapa --code-only
# Passos separados (o que o atalho faz por baixo):
#   omnigraph extract . && omnigraph cluster-only .

# O grafo interativo estará disponível localmente em:
# ./omnigraph-out/graph.html

# Perguntar, em português, como algo funciona (IA local sobre o mapa):
omnigraph-perguntar "como funciona o pagamento?"
# (a busca "Search nodes..." do gráfico só acha nós pelo nome; perguntas são aqui)

# Usar na IDE (Claude, Cursor, Copilot...) gastando menos tokens:
#   gera o mapa e registra o OmniGraph no seu assistente
omnigraph-ide

# Atualizar tudo para a última versão (rápido):
omnigraph-atualizar
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
