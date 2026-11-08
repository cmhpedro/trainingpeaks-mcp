# MCPs

Repo de servidores MCP (Model Context Protocol) para ferramentas que têm API mas não têm MCP oficial. Cada ferramenta vira um servidor independente, mas todos compartilham o mesmo projeto Python, a mesma stack e as mesmas convenções. Organização importa muito — siga estas regras à risca.

## Stack

- Python 3.14, gerenciado por Poetry (`poetry run poe <task>`); tools de sistema via `mise`
- SDK oficial do MCP (`mcp` 2.x), usando `MCPServer` (`from mcp.server.mcpserver import MCPServer`) — no 2.x o `FastMCP` foi renomeado; exemplos da internet com `mcp.server.fastmcp` são da 1.x
- httpx para falar com as APIs + `AutoRaiseTransport` (status >= 400 vira exceção)
- Pydantic v2 (models de entrada/saída) + pydantic-settings (credenciais e config)
- loguru (`src/log.py`) — **sempre em stderr**
- pytest + pytest-asyncio (testes `*_test.py` ao lado do código)
- ruff (`select = ["ALL"]`) + pyright

Comandos:

```bash
poetry run poe test    # pytest src
poetry run poe type    # pyright src
poetry run poe lint    # ruff check --fix src && ruff format src
poetry run python -m src.servers.<tool>                          # sobe o servidor em stdio
npx @modelcontextprotocol/inspector poetry run python -m src.servers.<tool>   # testa à mão no Inspector
```

Servidores existentes:

| Servidor | O que faz |
|---|---|
| `trainingpeaks` | Leitura de treinos, fitness, provas, saúde etc. do TrainingPeaks (ver `src/servers/trainingpeaks/README.md`) |

## Estrutura de pastas

```
pyproject.toml               um projeto só para todos os servidores
mise.toml                    tools (python) e tasks (`mise deploy`)
Dockerfile                   imagem única `mcp` (servidor escolhido pelo command do compose)
scripts/deploy.sh            build arm64 + envio por SSH + compose up no Pi
.env                         credenciais locais — NUNCA commitar (está no .gitignore)
.env.example                 todas as variáveis, sem valores, agrupadas por servidor
src/
  log.py                     instância única `logger` (sink em stderr)
  lib/                       utilitários genéricos, sem saber de nenhuma ferramenta
    http.py                   AutoRaiseTransport, ReadOnlyTransport, is_retryable_http_error
    retry.py                  decorator @retry (retenta só o que `when` aceitar)
    serve.py                  run(mcp): stdio ou streamable HTTP conforme MCP_TRANSPORT
    units.py                  conversões e formatação (pace, duração, km, km/h)
    utils.py                  today(), split_date_range, compact_json, downsample
  servers/
    <tool>/                  um pacote por ferramenta (nome em snake_case: `trello`, `todoist`)
      __main__.py             entrypoint: `mcp.run()` — permite `python -m src.servers.<tool>`
      server.py               instância `mcp = MCPServer("<tool>")` e as tools (camada de transporte)
      environment.py          settings da ferramenta (`env_prefix="<TOOL>_"`)
      client.py               cliente HTTP da API: um método por endpoint usado
      service.py              regra que combina várias chamadas (opcional)
      domain.py               models Pydantic e tipos da ferramenta
      conftest.py             fake da API (`httpx.MockTransport`) e `load_fixture`
      fixtures/               respostas da API em JSON usadas pelos testes
      *_test.py               testes
      README.md               o que o servidor faz, variáveis de ambiente, como registrar
```

### Regra de escopo

- Tipo/model de uma ferramenta → `src/servers/<tool>/domain.py`
- Helper genérico (HTTP, retry, paginação, log) → `src/lib/`
- Um servidor **nunca** importa de outro servidor. Se dois precisam da mesma coisa e ela é genérica, vai para `lib/`; se não é genérica, duplique.

## Camadas

Ordem de dependência, sem exceção: `server` → `service` → `client` → `domain`.

- **domain**: só models Pydantic e tipos (`Literal`, aliases). Sem I/O, sem lógica.
- **client**: dono do HTTP. Cliente httpx montado em `_build_client()` com `base_url`, auth e `transport=AutoRaiseTransport()` (envolvido em `ReadOnlyTransport` quando o MCP é só leitura); o construtor aceita `transport` para os testes injetarem o fake; métodos `async` decorados com `@retry(max_attempts=3)` (só em leitura/idempotente — nunca retentar `POST` que cria coisa). Resposta crua da API vira model de domínio por função módulo-privada `_to_<x>_model(raw)`; JSON cru nunca sai do client. Implemente só os endpoints que alguma tool usa — não espelhe a API inteira.
- **service**: só existe quando uma tool precisa de mais de uma chamada ou de regra (paginar até o fim, cruzar recursos, resolver nome → id). Recebe o client por construtor.
- **server**: só transporte. Declara as tools, valida entrada, chama service/client, devolve a resposta e traduz erros de domínio em `ToolError`. Nada de HTTP nem regra aqui. Recursos com ciclo de vida (o client httpx) nascem no `lifespan` do `MCPServer` e chegam à tool por `ctx: Context` (`ctx.request_context.lifespan_context`).

## Desenho das tools

Esta é a parte que mais importa: quem usa as tools é um LLM, não um programador.

- **Pense em tarefas, não em endpoints.** Uma tool deve responder a algo que o usuário pediria ("tarefas atrasadas do projeto X"), não ser um wrapper 1:1 de `GET /v2/items`. Poucas tools bem feitas > muitas tools rasas. Mire em até ~15 tools por servidor.
- **Nomes**: `snake_case`, verbo + objeto: `list_projects`, `get_task`, `create_task`, `search_tasks`. Consistentes entre servidores.
- **Descrição (docstring)**: é o prompt da tool. Diga o que faz, quando usar (e quando usar outra), formato/unidade dos parâmetros e o que volta. Em português, curta e objetiva.
- **Parâmetros**: tipados com `Annotated[T, Field(description=...)]`. Use `Literal` para valores fechados. Aceite **nome** onde der (ex.: `project: "Casa"`) e resolva para id no service — o LLM não deve precisar inventar ids. Datas em ISO 8601.
- **Saída**: model Pydantic enxuto, só com os campos úteis (nada de dump cru da API, URLs internas, metadados). Listas sempre paginadas/limitadas com `limit` (padrão sensato, ex.: 50) e um indicador `truncated`/`next_cursor` — nunca devolva milhares de itens.
- **Parâmetros só por nome**: depois de `ctx: Context`, use `*` — as tools não têm parâmetros posicionais.
- **Leitura vs escrita**: marque com `ToolAnnotations` de `mcp_types` (`read_only_hint`, `destructive_hint`, `idempotent_hint`). Tool destrutiva (delete, envio, pagamento) diz na descrição que é irreversível.
- **Erros**: erro da API vira exceção com mensagem acionável para o LLM ("projeto 'Casa' não encontrado; projetos disponíveis: ..."), não stack trace nem o JSON de erro cru. 401/403 → mensagem dizendo qual variável de ambiente conferir. **Só `ToolError` (`mcp.server.mcpserver.exceptions`) chega ao LLM com a mensagem** — qualquer outra exceção vira um genérico "Error executing tool X". O client levanta erros de domínio (`<Tool>Error` e subclasses) e o `server.py` os converte em `ToolError`.
- Se a ferramenta tem conceitos traiçoeiros (unidades, centavos, fuso, status), explique nas `instructions` do `MCPServer(...)` — elas vão para o contexto do cliente.

## Transporte e execução

- `__main__.py` chama `run(mcp)` de `src/lib/serve.py`, que escolhe o transporte por variável de ambiente:
  - padrão **stdio** — local, no Claude Code/Desktop do Mac;
  - `MCP_TRANSPORT=streamable-http` (o padrão da imagem Docker) — deploy remoto. Serve `/mcp` (stateless) e `/health`, escuta em `MCP_HOST:MCP_PORT` e só aceita os hostnames de `MCP_ALLOWED_HOSTS` + localhost (proteção contra DNS rebinding).
- **Em stdio, stdout é o canal do protocolo.** Nunca use `print` nem configure log em stdout — corrompe o JSON-RPC. Todo log vai pelo `logger` de `src/log.py`, que escreve em stderr.
- Os servidores **não têm autenticação própria**. Em produção a porta fica só no localhost do host e o acesso externo passa por um proxy com login que fale OAuth com os clientes MCP (ex.: Cloudflare Tunnel + Cloudflare Access com Managed OAuth). Nunca publique uma porta de MCP fora do localhost.

Registro local no Claude Code (stdio, path absoluto):

```bash
claude mcp add <tool> --scope user -- poetry --directory /caminho/para/trainingpeaks-mcp run python -m src.servers.<tool>
```

## Deploy

Uma imagem só (`mcp`, `Dockerfile` na raiz) com todos os servidores; o `compose.yml` do host escolhe o servidor no `command` (`["src.servers.<tool>"]`). A infra do host (compose, `.env` de cada servidor, proxy) fica **fora deste repo**; detalhes do ambiente de quem mantém o repo vão no `CLAUDE.local.md` (fora do git).

```bash
mise deploy   # testes + tipos + lint → build → docker save | ssh → compose up no host
```

- `scripts/deploy.sh` lê `DEPLOY_HOST` (destino SSH), `DEPLOY_DIR` (pasta do `compose.yml` no host) e `DEPLOY_PLATFORM` (padrão `linux/arm64`) do ambiente; defina no `mise.local.toml` (fora do git).
- **Nada da infra de quem mantém o repo entra no git** (hostnames, caminhos locais, repos privados, nomes de serviços): o repo é público. Isso vai no `CLAUDE.local.md` ou no `mise.local.toml`.
- Dependência nova com extensão nativa? Confirme que tem wheel `aarch64` para o Python 3.14.

## Credenciais e configuração

- Todo acesso a env var passa pelo `environment.py` do servidor (pydantic-settings, `env_prefix="<TOOL>_"`, `SecretStr` para tokens). Nada de `os.environ` espalhado.
- O `.env` fica na raiz do repo e é lido por path absoluto (resolvido a partir de `__file__`), porque o Claude Code sobe o servidor com outro diretório de trabalho.
- Credencial que expira (cookie, token manual) é lida a cada renovação, não só no boot: atualizar o `.env` não exige reiniciar o servidor.
- Variável nova entra no `.env.example` e no `README.md` do servidor.
- Nunca logue token, header de auth nem resposta com dado sensível.

## Convenções gerais

- **Import obrigatório**: `from src.log import logger` em todo arquivo (`required-imports` do ruff isort).
- **Async em tudo**: tools, service e client são `async`.
- **Enums**: `Literal[...]` no `domain.py`, não `Enum`.
- **Dinheiro**: inteiro em centavos, nunca float, com a unidade explícita no nome do campo (`amount_in_cents`).
- **Data/hora**: sempre com timezone; converta o que a API mandar para o fuso local na saída.
- **Privacidade**: tudo que não é exportado leva `_` (conversores, helpers, `_build_client`).
- **Nomes**: models `<Entidade>Model`; classes `<Tool>Client` / `<Tool>Service`; settings `<Tool>Settings`.
- **Lint**: `select = ["ALL"]`. `# noqa` só com código específico e motivo claro. Não relaxe regra no `pyproject.toml` para fazer código passar. Docstrings são um resumo curto (sem seções Args/Returns, `DOC` ignorado); em `server.py` a docstring da tool é a descrição para o LLM, por isso `D205`/`D401` não se aplicam lá.
- **Testes de float**: compare com `pytest.approx`.
- **Dependências**: antes de adicionar uma lib, veja se httpx + Pydantic resolvem. SDK oficial da ferramenta só se a API for complicada de verdade (auth exótica, assinatura de request).

## Testes

- Arquivo `<modulo>_test.py` ao lado do código, factory `_make_sut`, variável `sut`, blocos `# Arrange` / `# Act` / `# Assert`, todo teste `@pytest.mark.asyncio`.
- **Mock proibido** (`unittest.mock`, `pytest-mock`). Para não bater na API real nos testes: fake da API em `conftest.py` servido por `httpx.MockTransport` (injetado no `transport` do client), com respostas reais gravadas em `fixtures/` (JSON copiado da API, com dados sensíveis trocados). Throttle/espera do client devem ser configuráveis para os testes rodarem com zero.
- O que testar: conversão resposta → model (`_to_<x>_model`), regra do service (paginação, resolução de nome), tradução de erro. As tools em si se testam em memória com o client do SDK: `async with Client(mcp) as client` (`from mcp import Client`).
- Teste contra a API real (quando valer) fica marcado com `@pytest.mark.integration` e fora do `poe test` padrão.

## Checklist para um MCP novo

1. Ler a documentação da API e listar **as tarefas** que o MCP precisa resolver — é delas que saem as tools, não da lista de endpoints. Na dúvida sobre escopo, pergunte antes de implementar.
2. Criar `src/servers/<tool>/` com `environment.py` (credenciais) e adicionar as variáveis no `.env.example`.
3. `domain.py` com os models de saída enxutos.
4. `client.py` com só os endpoints necessários, `_build_client()`, `@retry` nas leituras e `_to_<x>_model`.
5. `service.py` se alguma tool combina chamadas ou resolve nome → id.
6. `server.py` com `MCPServer("<tool>", instructions=..., lifespan=...)` e as tools seguindo **Desenho das tools**; `__main__.py` chamando `mcp.run()`.
7. Testes seguindo **Testes**, com fixtures gravadas da API.
8. `README.md` do servidor: o que faz, lista de tools, variáveis de ambiente, comando `claude mcp add`; e uma linha na tabela **Servidores existentes** deste arquivo.
9. Rodar `poetry run poe test`, `poetry run poe type` e `poetry run poe lint` — nessa ordem, tudo verde.
10. Para publicar: serviço no compose do host e hostname no proxy (ver **Deploy** e o `CLAUDE.local.md`), depois `mise deploy`.
11. Abrir no MCP Inspector e chamar cada tool pelo menos uma vez contra a API real (leitura; escrita só com confirmação do dono da conta).
