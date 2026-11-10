# TrainingPeaks MCP

Acesso **somente leitura** ao TrainingPeaks do atleta: treinos, análises, fitness
(CTL/ATL/TSB), recordes, provas, calendário, saúde, equipamentos, treinos de força
e biblioteca de treinos. Baseado no mapeamento da API feito em
[JamsusMaximus/trainingpeaks-mcp](https://github.com/JamsusMaximus/trainingpeaks-mcp),
sem nenhuma das operações de escrita.

## Como funciona a autenticação

A API oficial do TrainingPeaks exige aprovação, então este servidor usa a mesma API
do site: troca o cookie de sessão `Production_tpAuth` por um bearer token
(`GET /users/v3/token`), que fica em memória até expirar.

O cookie expira de tempos em tempos. Quando isso acontece as tools respondem com
instruções; basta atualizar o `.env` (não precisa reiniciar o servidor):

1. Faça login em <https://app.trainingpeaks.com>
2. DevTools → Application → Cookies → `https://app.trainingpeaks.com`
3. Copie o valor de `Production_tpAuth` para `TRAININGPEAKS_AUTH_COOKIE` no `.env`
   da raiz do repo

## Garantia de somente leitura

Além de nenhuma tool escrever, o client monta o httpx com `ReadOnlyTransport`
(`src/lib/http.py`): qualquer método que não seja de leitura é bloqueado antes de
sair da máquina. As únicas exceções são os `POST` que a API usa para consultar,
listados em `_READ_ONLY_POST_PATHS` no `client.py`:

- `/fitness/v1/athletes/{id}/reporting/performancedata/{início}/{fim}` (curva de fitness)
- `/workout-analysis/v2/analyze/{summary,charts,laps}` (análise de treino)

## Variáveis de ambiente

| Variável | Descrição |
|---|---|
| `TRAININGPEAKS_AUTH_COOKIE` | Valor do cookie `Production_tpAuth` |

## Tools

| Tool | O que devolve |
|---|---|
| `get_athlete_profile` | Dados pessoais, conta, limiares e zonas (potência, FC, pace), calorias, piscinas |
| `list_workouts` | Treinos de um período (planejados/realizados), com totais por esporte e o tênis/bike usado |
| `get_workout` | Detalhe do treino: planejado vs realizado, estrutura, comentários, nota privada, arquivos, recordes, tênis/bike |
| `get_workout_analysis` | Totais, canais com zonas, voltas e amostra da série temporal |
| `list_strength_workouts` | Treinos do construtor de força (não aparecem em `list_workouts`) |
| `get_strength_workout` | Blocos, exercícios e séries (prescrito vs executado) |
| `get_fitness` | TSS, CTL, ATL e TSB por dia + forma atual |
| `get_week_summary` | Semana seg-dom: planejado vs realizado, totais, força e fitness |
| `get_personal_records` | Recordes de potência, FC e tempos de corrida |
| `get_annual_training_plan` | ATP: TSS semanal planejado, períodos e provas |
| `get_events` | Provas do período + prova em foco + próxima prova |
| `list_calendar_notes` | Notas do calendário com comentários |
| `list_availability` | Indisponibilidades e disponibilidade limitada |
| `get_health_metrics` | Peso, HRV, sono, FC de repouso etc. e nutrição |
| `list_equipment` | Bikes e tênis com quilometragem total (já somando a inicial) e limite de troca |
| `list_workout_libraries` | Bibliotecas de treinos-modelo |
| `list_library_workouts` | Treinos-modelo de uma biblioteca |
| `get_library_workout` | Treino-modelo com estrutura de intervalos |

Fora de propósito: funções de treinador (grupos, lista de atletas, planos de
treino de autoria própria), download de arquivos FIT e a sondagem de métodos de
zona, que depende de chamar a calculadora de zonas.

## Uso

Local (stdio):

```bash
claude mcp add trainingpeaks --scope user -- poetry --directory /caminho/para/trainingpeaks-mcp run python -m src.servers.trainingpeaks
```

Remoto (streamable HTTP, imagem Docker atrás de um proxy com login; ver o README da raiz).
No container o cookie vem da variável de ambiente, e trocar exige recriar o container.

```bash
claude mcp add --transport http --scope user trainingpeaks https://<seu-hostname>/mcp
```

## Pendências

- As fixtures de teste foram montadas a partir dos formatos documentados no projeto
  de referência, não de respostas gravadas. Troque por respostas reais.
- Conferir com dados reais: unidade do `value` dos recordes de velocidade (assumido
  m/s), nomes de campos de velocidade/cadência do treino (`velocityAverage` etc.) e
  o formato das linhas da série temporal da análise.
