# Rodar o servidor MCP do omnigraph

O `omnigraph-mcp` expõe as ferramentas de consulta ao grafo (caminho entre nós,
explicação de um nó, navegação por comunidades) para um assistente de IA via
protocolo MCP. Ele serve um `graph.json` já extraído — então o fluxo é sempre
**extrair primeiro, servir depois**.

## 1. Gerar o grafo

Na raiz do projeto que você quer mapear:

```bash
omnigraph extract .          # AST + passe semântico; gera omnigraph-out/graph.json
omnigraph update .           # reextrai só o código e atualiza o grafo (sem LLM)
```

O resultado fica em `omnigraph-out/graph.json` (ou no caminho de `OMNIGRAPH_OUT`).

## 2. Servir por stdio (padrão, um dev)

É o transporte que o Claude e outros agentes usam localmente — o assistente
sobe o processo e conversa por stdin/stdout. Não abre porta nenhuma.

```bash
omnigraph-mcp                              # lê omnigraph-out/graph.json
omnigraph-mcp caminho/para/graph.json      # ou um grafo específico
```

Normalmente você não roda isso à mão: o `omnigraph install` registra a skill no
assistente, que sobe o servidor sozinho quando precisa.

## 3. Servir por HTTP (serviço compartilhado)

Para vários clientes apontando ao mesmo grafo — uma máquina de time, um
container. Sobe um servidor HTTP com os mesmos recursos do stdio.

```bash
omnigraph-mcp graph.json --transport http --host 127.0.0.1 --port 8080
```

Opções que importam:

| Flag | Padrão | Para quê |
|---|---|---|
| `--host` | `127.0.0.1` | Interface de escuta. `0.0.0.0` expõe na rede — só com chave |
| `--port` | `8080` | Porta HTTP |
| `--path` | `/mcp` | Caminho de montagem: `http://host:porta/mcp` |
| `--api-key` | — | Exige a chave em toda requisição (ou via `OMNIGRAPH_API_KEY`) |
| `--stateless` | desligado | Sem estado por sessão, para balanceador de carga / CI |

**Expôs na rede? Use chave.** Com `--host 0.0.0.0` sem `--api-key`, qualquer um
na rede consulta o grafo.

```bash
export OMNIGRAPH_API_KEY="uma-chave-secreta"
omnigraph-mcp graph.json --transport http --host 0.0.0.0 --port 8080
```

## 4. Em Docker

O `Dockerfile` da raiz empacota o servidor HTTP. Monte o diretório com o grafo
em `/data`:

```bash
docker build -t omnigraph-mcp .
docker run --rm -p 8080:8080 -v "$PWD/omnigraph-out:/data" \
  omnigraph-mcp /data/graph.json --transport http --host 0.0.0.0 --port 8080
```

O container roda como usuário sem privilégio e escuta em `:8080`.

## Resumo do fluxo

```
omnigraph extract .                          → omnigraph-out/graph.json
        │
        ├─ omnigraph-mcp                      → stdio, para o assistente local
        └─ omnigraph-mcp … --transport http   → HTTP, serviço compartilhado
```
