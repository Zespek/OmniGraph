<div align="left">
  <img src="https://readme-typing-svg.herokuapp.com/?font=Fira+Code&weight=600&size=22&pause=2500&duration=2000&color=bd00ff&vCenter=true&width=800&height=40&lines=>_+./politica_de_seguranca.sh" alt="Política de Segurança" />
</div>

```ini
zespek@server:~$ cat ./politica_seguranca.ini
[ Diretrizes de Segurança, Versões Suportadas e Reporte de Vulnerabilidades ]

[Versoes_Suportadas]
  - "0.3.x  : Sim (Ativa)"
  - "< 0.3  : Não"

[Como_Reportar_Vulnerabilidade]
  - "Por favor, NÃO abra uma issue pública no GitHub para relatar falhas de segurança."
  - "Reporte vulnerabilidades usando a função privada de reporte do GitHub"
  - "ou envie um e-mail diretamente para o desenvolvedor em joezika2016@gmail.com."
```

---

<div align="left">
  <img src="https://readme-typing-svg.herokuapp.com/?font=Fira+Code&weight=600&size=22&pause=2500&duration=2000&color=bd00ff&vCenter=true&width=800&height=40&lines=>_+./modelo_de_seguranca.sh" alt="Modelo de Segurança" />
</div>

```ini
zespek@server:~$ cat ./modelo_seguranca.ini
[ O OmniGraph funciona como ferramenta local de desenvolvimento. ]
[ Não abre portas ou faz chamadas externas sem consentimento explícito. ]

[Superficie_de_Ameacas_e_Mitigacoes]
  - "SSRF via URL Fetch        : Validamos esquemas http/https, bloqueamos IPs privados/loopback e metadados de nuvem."
  - "Downloads Excessivos      : Fluxo de fetch aborta automaticamente se atingir 50 MB (texto em 10 MB)."
  - "Path Traversal (MCP)     : Sanitização rigorosa exige caminhos restritos à pasta 'omnigraph-out/'."
  - "Ataques de XSS em Grafos   : Escapamos todos os títulos e nomes de nós antes da renderização HTML do pyvis."
  - "Injeções de Prompt (LLM)  : Envelopamos arquivos em blocos delimitadores e aplicamos filtros contra jailbreaks."
  - "Erros de Codificação       : Decodificação robusta UTF-8 com fallback de substituição para evitar crashes."
```

---

<div align="left">
  <img src="https://readme-typing-svg.herokuapp.com/?font=Fira+Code&weight=600&size=22&pause=2500&duration=2000&color=bd00ff&vCenter=true&width=800&height=40&lines=>_+./o_que_o_sistema_nao_faz.sh" alt="O que o sistema não faz" />
</div>

```ini
zespek@server:~$ cat ./limites_do_sistema.ini
[ O OmniGraph opera dentro de limites restritos para garantir total privacidade: ]

[Diretrizes_Limites]
  - "Não executa servidores de escuta externa por padrão (comunicação via stdio)."
  - "Não executa código dos arquivos analisados (apenas gera a árvore sintática via AST)."
  - "Não utiliza subprocessos com shell=True ativo."
  - "Não armazena chaves de API ou credenciais de usuários."
```

---

<div align="center">
  <p>Mantido com foco em segurança de dados e privacidade por <b><a href="https://github.com/Zespek">Zespek (José Felipe)</a></b></p>
</div>
