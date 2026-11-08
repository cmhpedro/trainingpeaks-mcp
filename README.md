# TrainingPeaks MCP

Servidor [MCP](https://modelcontextprotocol.io) **somente leitura** para o
[TrainingPeaks](https://www.trainingpeaks.com). Com ele, Claude, Grok ou qualquer outro cliente MCP
consultam os seus treinos, a análise de cada treino, fitness (CTL/ATL/TSB), recordes, provas,
calendário, métricas de saúde, equipamentos, treinos de força e a biblioteca de treinos, sem
nunca alterar nada na sua conta.

> **Projeto não oficial.** O TrainingPeaks só libera a API oficial para parceiros aprovados, por
> isso este servidor usa a mesma API interna do site, autenticada pelo cookie da sua sessão. Ela
> pode mudar sem aviso, e o uso é por sua conta e risco.

O mapeamento da API veio do [JamsusMaximus/trainingpeaks-mcp](https://github.com/JamsusMaximus/trainingpeaks-mcp).
A diferença é que aqui só existem operações de leitura.

## O que dá para perguntar

- "Como foi minha semana de treinos? Cumpri o planejado?"
- "Qual é a estrutura do intervalado de terça?"
- "Como está meu CTL e minha forma para a prova do mês que vem?"
- "Quais são meus melhores tempos de 5 km e 10 km este ano?"
- "Como variou minha FC na corrida longa de domingo?"
- "Quantos km meu tênis atual já tem?"

São 18 tools, todas marcadas como somente leitura. A lista completa, com o que cada uma devolve,
está em [`src/servers/trainingpeaks/README.md`](src/servers/trainingpeaks/README.md#tools).

## Garantia de somente leitura

Não basta nenhuma tool escrever: o cliente HTTP é montado com um transporte (`ReadOnlyTransport`,
em [`src/lib/http.py`](src/lib/http.py)) que bloqueia qualquer `POST`/`PUT`/`DELETE` antes de a
requisição sair da máquina. A única exceção é uma lista fechada de `POST` que a API usa para
*consultar* dados, como a curva de fitness e a análise de treino.

## Requisitos

- Python 3.14 e [Poetry](https://python-poetry.org) 2.x. Com [mise](https://mise.jdx.dev), basta
  `mise install`.
- Uma conta TrainingPeaks. Os dados de fitness e a análise detalhada dependem dos recursos da sua
  assinatura.

## Configuração

1. Instale as dependências:

   ```bash
   git clone https://github.com/cmhpedro/trainingpeaks-mcp.git
   cd trainingpeaks-mcp
   poetry install        # ou: mise install
   ```

2. Pegue o cookie de sessão:
   1. Faça login em <https://app.trainingpeaks.com>.
   2. Abra o DevTools → **Application** → **Cookies** → `https://app.trainingpeaks.com`.
   3. Copie o valor de `Production_tpAuth`.

3. Crie o `.env` na raiz do repo:

   ```bash
   cp .env.example .env
   # TRAININGPEAKS_AUTH_COOKIE=<valor do cookie>
   ```

O cookie expira de tempos em tempos. Quando isso acontecer, as tools respondem explicando como
renová-lo. Basta atualizar o `.env`: o valor é relido a cada renovação de token, então não precisa
reiniciar o servidor.

## Uso local (stdio)

**Claude Code:**

```bash
claude mcp add trainingpeaks --scope user -- \
  poetry --directory /caminho/para/trainingpeaks-mcp run python -m src.servers.trainingpeaks
```

**Claude Desktop** (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "trainingpeaks": {
      "command": "poetry",
      "args": [
        "--directory", "/caminho/para/trainingpeaks-mcp",
        "run", "python", "-m", "src.servers.trainingpeaks"
      ]
    }
  }
}
```

**MCP Inspector**, para testar as tools à mão:

```bash
npx @modelcontextprotocol/inspector poetry run python -m src.servers.trainingpeaks
```

## Uso remoto (HTTP)

A imagem Docker sobe o servidor em *streamable HTTP*, com o endpoint em `/mcp` e um health check em
`/health`:

```bash
docker build -t trainingpeaks-mcp .
docker run --rm -p 127.0.0.1:8000:8000 \
  -e TRAININGPEAKS_AUTH_COOKIE=<cookie> \
  -e MCP_ALLOWED_HOSTS=mcp.seudominio.com \
  trainingpeaks-mcp src.servers.trainingpeaks
```

| Variável | Padrão na imagem | Descrição |
|---|---|---|
| `TRAININGPEAKS_AUTH_COOKIE` | — | Valor do cookie `Production_tpAuth` |
| `MCP_TRANSPORT` | `streamable-http` | `stdio` ou `streamable-http` (fora do Docker o padrão é `stdio`) |
| `MCP_HOST` / `MCP_PORT` | `0.0.0.0` / `8000` | Onde o servidor escuta |
| `MCP_ALLOWED_HOSTS` | — | Hostnames públicos aceitos, separados por vírgula. Proteção contra DNS rebinding: qualquer outro `Host` recebe 421 |

> ⚠️ **O servidor não tem autenticação própria.** Quem alcança a URL lê todos os seus dados do
> TrainingPeaks. Nunca exponha a porta diretamente: coloque um proxy com login na frente.

Uma forma que funciona bem é o Cloudflare Tunnel com o **Cloudflare Access** e o *Managed OAuth*,
o fluxo OAuth que os clientes MCP entendem:

- a porta fica só no `127.0.0.1` do host e é publicada pelo túnel;
- uma application *self-hosted* do Access cobre o hostname, com uma policy *Allow* só para o seu
  e-mail;
- o Managed OAuth fica ligado e as *Allowed redirect URIs* incluem
  `https://claude.ai/api/mcp/auth_callback` e `https://claude.com/api/mcp/auth_callback`;
- *Protect with Access* fica ligado na rota do túnel, para o `cloudflared` validar o JWT do Access
  (team name + AUD tag);
- se o domínio tiver regras de WAF com challenge (por país, Bot Fight Mode etc.), abra uma exceção
  para o hostname. O Claude conecta a partir dos servidores da Anthropic, que não resolvem
  challenge. O Access continua barrando quem não fez login.

No Claude, o conector é adicionado em **Customize → Connectors → Add custom connector**, com a URL
`https://<hostname>/mcp`, *Sign in now* e o OAuth client **Register automatically**.

## Desenvolvimento

```bash
poetry run poe test    # pytest
poetry run poe type    # pyright
poetry run poe lint    # ruff check --fix + ruff format
```

Os testes não batem na API real: um fake servido por `httpx.MockTransport` responde com as fixtures
JSON de `src/servers/trainingpeaks/fixtures/`.

O repo foi pensado para abrigar mais de um servidor MCP, por isso o código fica em
`src/servers/<ferramenta>/`, com utilitários compartilhados em `src/lib/`. As convenções (camadas,
desenho das tools, testes) estão no [`CLAUDE.md`](CLAUDE.md).

```
src/
  lib/                     HTTP somente leitura, retry, transporte (stdio/HTTP), unidades
  servers/trainingpeaks/
    server.py              tools MCP (camada de transporte)
    service.py             regras que combinam várias chamadas
    client.py              cliente da API + conversão para os models
    domain.py              models Pydantic de saída
```

## Créditos

- [JamsusMaximus/trainingpeaks-mcp](https://github.com/JamsusMaximus/trainingpeaks-mcp), pelo
  mapeamento da API do TrainingPeaks.

## Licença

[MIT](LICENSE)
