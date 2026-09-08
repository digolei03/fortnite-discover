# Fortnite Discover Lab

Pipeline de coleta e análise da colocação de ilhas no Discover do Fortnite.

## Por que existe

As decisões de portfólio do estúdio vinham de leitura de capturas de tela pontuais.
Isso produz conclusões erradas: uma correlação de rank contra volume de jogadores
deu `rho +0,96` no horror mas `-0,17` no shooter — ou seja, a "regra" era apenas
overfit a uma foto de 10 jogos.

Três problemas tornam a leitura por print inviável, e o pipeline resolve os três:

1. **Amostra truncada** — a página de gênero mostra só o top 50. O endpoint de
   histórico não tem esse teto.
2. **Sem direção temporal** — impossível separar "volume causa posição" de
   "posição causa volume". Com série diária isso vira testável.
3. **Volatilidade extrema** — `BACKROOMS: BEST LEVELS` saiu de **#7 para #227**
   em poucos dias, passando por #7 → #16 → #28 → #40 → #51 → #99 **em seis horas**.
   Uma foto por semana não veria nada disso.

## O achado que define o projeto

O fortnite.gg expõe um endpoint com o **histórico completo de colocação no
Discover**, por ilha, em resolução horária, desde abril de 2026:

```
GET /player-count-discovery?range=0&id=<fgg_id>&type=
-> [["Horror", 1757320200, 1757323800, 7], ...]
      row do Discover, inicio, fim, posicao na row
```

Isso significa que **a análise não precisa esperar semanas acumulando série** —
há ~5 meses de backfill disponíveis para a variável dependente. Rows observadas
até agora: `Horror`, `Top Rated`, `Popular`, `Sponsored`, `First Person Islands`,
`New`.

## Fontes

| fonte | o que dá | histórico |
|---|---|---|
| `api.fortnite.com/ecosystem/v1` | métricas oficiais da Epic, incluindo `favorites`, `recommendations`, `plays` | **~7 dias, sem backfill** |
| `fortnite.gg/player-count-discovery` | colocação no Discover, por row, horária | ~5 meses |
| `fortnite.gg/player-count-graph` | CCU diário | ~5 meses |
| `fortnite.gg/genre/<slug>` | top 50 do gênero + movers até ~#300 | só o dia corrente |
| `fortnite.gg/genres` | agregados por gênero, incluindo **$/player** | só o dia corrente |

**A janela de 7 dias da Epic é o motivo de a coleta ser diária e obrigatória.**
Cada dia sem coletar é um dia perdido para sempre.

## Uso

```bash
pip install -e .

python -m collectors.ranks       # top 50 dos 13 generos + agregados
python -m collectors.islands     # resolve island_code -> fgg_id (so as novas)
python -m collectors.history     # colocacao + ccu historicos
python -m collectors.metrics     # metricas diarias da Epic

python -m analysis.run                          # todas as analises
python -m analysis.run --only q1                # so a de causalidade
python -m analysis.run --island 1234-5678-9012  # ficha de uma ilha
```

Primeira execução: `collectors.islands` leva ~18 min para ~1.200 ilhas (uma
requisição por ilha para descobrir o `fgg_id`). Depois disso só resolve as novas.
Para o backfill completo, rode `python -m collectors.history --scope all` uma vez.

**Antes de tudo:** preencher `config/our_islands.txt` com os códigos das ilhas do
estúdio. Elas são coletadas todo dia mesmo fora do top 50 — sem isso o Rainbow
Friends não é rastreado quando cai abaixo de #50.

## As três perguntas

- **Q1 — causalidade** (`analysis/q1_causality.sql`). Compara "trafégo hoje prevê
  posição amanhã" contra "posição hoje prevê tráfego amanhã". Se a segunda
  dominar, otimizar métricas para subir no rank é ilusão: a posição é a causa,
  não o efeito.
- **Q2 — o que move a posição** (`analysis/run.py`). Regressão com efeito fixo por
  ilha, rodada separadamente por row. O efeito fixo faz cada ilha ser seu próprio
  controle, o que elimina o confundidor "essa ilha é grande" e a restrição de
  amplitude de qualquer análise feita num top-N já selecionado.
- **Q3 — duração dos destaques** (`analysis/q3_spikes.sql`). Identifica saltos de
  30+ posições e mede quantos dias o jogo sustenta, cruzando com retenção e taxa
  de favoritagem. É o que permite prever se um destaque recebido hoje vai decair.

A hipótese central do Q2 são os três campos que **só a API da Epic tem**:
`favorites`, `recommendations` e `plays`. A documentação da Epic diz que as rows
pontuam sinais de engajamento, retenção, **social** e similaridade — e nenhuma
ferramenta pública analisa os candidatos diretos ao termo social.

## Detalhes que custaram tempo para descobrir

- **`24h Avg Playtime` do fortnite.gg ≠ `averageMinutesPerPlayer` da Epic.**
  Para Murder Mystery: 20,2 contra 54,5. A razão é exatamente `plays/unique_players`
  (2,69 sessões). O fortnite.gg mede por **sessão**, a Epic por **jogador/dia**.
  Não misture as duas.
- **O rank no HTML vem colado com o delta.** `<div class='genre-rank-wrap'>#7<div
  class='genre-rank-delta down'>1</div></div>` lido inteiro vira `#71`. O parser
  remove o nó do delta antes de ler o rank.
- **A página de gênero não pagina.** `?page`, `?offset`, `?start`, `?limit` são
  todos ignorados. São 50 linhas, ponto.
- **fortnite.gg está atrás de Cloudflare.** `requests`/`httpx` tomam 403;
  `curl_cffi` com `impersonate="chrome"` passa.
- **`from`/`to` na API da Epic** são aceitos pela validação (ISO 8601 obrigatório)
  mas devolveram série vazia nos testes. Chamamos sem parâmetros.
- **`/island?id=X&ranks`** devolve a posição da ilha no ranking **global** de
  10 métricas (não o rank de gênero). Não é usado hoje, mas é uma fonte extra.
- **`retention` da Epic é fração** (0.39), o fortnite.gg mostra percentual (39%).

## Armazenamento

Parquet particionado, commitado no repo. Git dá versionamento e auditabilidade —
num estudo cuja pergunta central é "o que mudou e quando", isso vale mais que ter
banco. Migrar para BigQuery/Postgres quando quiserem dashboard.

```
data/
├── raw_html/dt=YYYY-MM-DD/<slug>.html.gz   # sempre salvo, para reprocessar
├── ranks/dt=YYYY-MM-DD/                    # top 50 por genero
├── movers/dt=YYYY-MM-DD/                   # climbers/fallers/novos, ate ~#300
├── genres/dt=YYYY-MM-DD/                   # agregados por genero
├── metrics/dt=YYYY-MM-DD/                  # Epic API
├── placements/ym=YYYY-MM/                  # colocacao historica (particao mensal)
├── players_daily/ym=YYYY-MM/               # ccu historico
└── islands/islands.parquet                 # dimensao
```

O HTML bruto é salvo sempre. O parser **vai** quebrar quando o fortnite.gg mudar
o layout; com o HTML guardado, é só reprocessar em vez de perder o dia.

## Não existe "o rank" — existe uma distribuição

Isto começou como premissa não verificada e foi **confirmado** pela assinatura da
API oficial da Epic. A Discover é servida por:

```
POST fn-service-discovery-live-public.ogs.live.on.epicgames.com
     /api/v2/discovery/surface/CreativeDiscoverySurface_Frontend
     ?appId=Fortnite&stream=<branch>
```

com um corpo que é um **perfil de jogador**:

```json
{ "playerId": "...", "matchmakingRegion": "BR|NAE|EU", "platform": "...",
  "locale": "...", "rating": "TEEN", "ratingAuthority": "ESRB",
  "isCabined": false, "numLocalPlayers": 1 }
```

e uma resposta que traz `panels[]` mais **`testVariantName` / `testName` /
`testAnalyticsId`** — a Epic roda testes A/B na Discover e informa em qual
variante aquela chamada caiu.

**Portanto a posição varia por playerId, região, plataforma, locale, rating e
variante de teste.** O número do fortnite.gg é *uma* leitura dessa distribuição.
Qualquer análise de posição neste repositório precisa dizer de qual perfil o
número veio — e a boa notícia é que a variante vem no dado, então dá para
controlar por ela em vez de apenas constatar o problema.

O `collectors/uefn_exposure.py` lê essa fonte (via uefntoolkit), com as
dimensões explícitas em `data/exposure_targets`. O caminho do fortnite.gg
continua valendo como referência cruzada e pelo histórico retroativo.

### Risco de conta

Chamar a API de Discovery exige device auth de uma conta Epic e as chamadas são
feitas como se fossem de um jogador. **Use uma conta secundária, nunca a
principal do estúdio**, e mantenha a cadência baixa.
