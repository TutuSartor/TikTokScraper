# Product Intelligence

Sistema pessoal para registrar candidatos a produto, suas fontes e evidências datadas, e apoiar a triagem para testes numa loja Shopify voltada aos EUA. O desenho completo está em [ARCHITECTURE.md](ARCHITECTURE.md).

**Estado atual: Fase 1 concluída; Fase 2 em andamento.** Existem projeto Python, Docker Compose com PostgreSQL, FastAPI com `/health`, Alembic, configuração por `.env`, testes, scripts de backup e o **esquema do banco da Fase 2** (migrações `0002` e `0003`). Também já existem a **importação CSV** (`ingest/` e `jobs/import_csv.py`) e as **rotas da API** para candidatos, fontes e observações (`api/`). Ainda **não** há coleta TikTok, dashboard nem score.

## Estrutura

```text
.
├── ARCHITECTURE.md
├── README.md
├── pyproject.toml          # dependências e config de pytest/ruff
├── Dockerfile              # alvos: runtime (API) e dev (testes)
├── compose.yaml            # db (PostgreSQL 16) + api; serviço "tests" no perfil test
├── .env.example
├── alembic.ini
├── src/product_intelligence/
│   ├── main.py             # create_app() / app
│   ├── config.py           # Settings (DATABASE_URL, APP_ENV, LOG_LEVEL)
│   ├── domain.py           # regras de gravação comuns à API e ao CSV
│   ├── api/                # rotas: health, candidates, sources (+ schemas, deps)
│   ├── db/                 # Base, engine/sessão, models.py (tabelas) e enums.py (vocabulários)
│   ├── ingest/             # importação CSV: urls.py, rows.py, importer.py
│   ├── analytics/          # Fases 3–4 (vazio)
│   └── jobs/               # comandos: import_csv.py
├── migrations/             # Alembic — 0001_baseline, 0002_domain_tables, 0003_observation_integrity
├── scripts/                # backup.sh / restore.sh
└── tests/                  # health, config, backup, migrações e integridade do esquema
```

## Pré-requisitos na VM

- Linux com Docker Engine e o plugin Docker Compose v2 (`docker compose version`).
- Git. Não é necessário Python no host para rodar a aplicação — só para desenvolvimento local.
- Nenhuma conta TikTok ou chave Shopify é necessária nesta fase.

## 1. Configurar o `.env`

```bash
git clone <seu-repo> /opt/product-intelligence
cd /opt/product-intelligence
cp .env.example .env
nano .env        # troque POSTGRES_PASSWORD por uma senha forte
chmod 600 .env
```

| Variável | Uso |
|---|---|
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | Credenciais do banco e da API, passadas separadamente para aceitar caracteres especiais na senha. |
| `POSTGRES_HOST`, `POSTGRES_PORT` | Fora do Docker: `localhost` e `5432` por padrão. O Compose define o host como `db`. |
| `DATABASE_URL` | Alternativa para desenvolvimento local; quando definida, tem prioridade sobre os campos `POSTGRES_*`. Codifique caracteres especiais da URL. |
| `APP_ENV` | `development`, `test` ou `production`. |
| `LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`… |
| `API_PORT` | Porta publicada em `127.0.0.1` no host (padrão 8000). |

O `.env` está no `.gitignore` — nunca versione segredos.
Para senhas com `$`, `#` ou espaços, use aspas simples no `.env`, por exemplo `POSTGRES_PASSWORD='senha$com#caracteres'`. Os scripts usam o ambiente do container e não executam esse arquivo como shell.

## 2. Iniciar

```bash
docker compose up --build -d
docker compose ps                  # db e api devem ficar "healthy"
curl http://127.0.0.1:8000/health
# {"status":"ok","database":"ok","version":"0.1.0"}
```

Ao subir, a API executa `alembic upgrade head` antes do Uvicorn; se a migração falhar, o container não inicia (ver `docker compose logs api`). `/health` devolve **200** apenas se o banco responder a `SELECT 1`, e **503** caso contrário. A documentação interativa fica em `http://127.0.0.1:8000/docs`.

## 3. Migrações

```bash
docker compose exec api alembic current          # revisão aplicada
docker compose exec api alembic upgrade head     # aplicar pendentes
docker compose exec api alembic history
```

Criar uma nova revisão (a partir da Fase 2), em ambiente de desenvolvimento com o código montado localmente:

```bash
alembic revision --autogenerate -m "create product_candidate"
```

Convenções: modelos herdam de `product_intelligence.db.Base` e ficam em `src/product_intelligence/db/models.py` (o `migrations/env.py` importa esse módulo automaticamente). Revise sempre o arquivo gerado antes de commitar. Deve existir um único *head* — um teste falha se houver dois — e `tests/test_models.py` falha se os modelos divergirem das migrações ou se um valor novo de enum não estiver no CHECK do banco.

## Esquema do banco (Fase 2)

Tabelas criadas pela migração `0002` a partir do ARCHITECTURE.md §4, mais duas de auditoria de importação exigidas pela §5:

| Tabela | Papel | Regras garantidas pelo banco |
|---|---|---|
| `product_candidate` | conceito de produto | nome não vazio; `status` ∈ new, watch, research_supplier, test, reject |
| `source_item` | vídeo, anúncio, tendência ou fornecedor | único por (`source_type`, `external_url`) |
| `product_source` | liga candidato ↔ fonte (`relation`, `reviewed_at`) | apagada em cascata com o candidato ou a fonte |
| `observation` | snapshot datado das métricas disponíveis | único por (`source_item_id`, `observed_at`); contagens e preço ≥ 0; métrica ausente = `NULL`; região com 2 letras maiúsculas; coleta CSV exige lote e número de linha ≥ 2 (cabeçalho é linha 1); filtro americano exige região `US` |
| `supplier_offer` | cotação datada de fornecedor | valores ≥ 0; se houver moeda original, exige taxa > 0 e data da taxa |
| `review` | decisão humana | motivo obrigatório |
| `score_run` | resultado de regra versionada | `coverage` entre 0 e 1; `score` pode ser `NULL` |
| `import_batch` | um arquivo importado (nome, SHA-256, contadores, status) | aceitas + rejeitadas ≤ total |
| `import_row_error` | erro de uma linha do arquivo | linha ≥ 1 |

Histórico não se apaga por acidente: fonte com observações e candidato com review, cotação ou score não podem ser removidos (FK `RESTRICT`). Os vocabulários ficam em `db/enums.py` e são gravados como texto com CHECK, o que funciona em PostgreSQL e SQLite. Para acrescentar um valor, crie uma migração que recrie o CHECK.

A migração `0003` bloqueia UPDATE e DELETE de observações por triggers, inclusive em SQL direto. Novas coletas devem gerar novos snapshots. As exclusões pelo ORM respeitam as mesmas regras de CASCADE e RESTRICT do banco, inclusive com associações já carregadas.

Datas fornecidas aos modelos precisam incluir fuso horário (`Z` ou offset, por exemplo `-03:00`); são normalizadas e devolvidas em UTC. A validação de fuso acontece na camada Python; SQL direto continua seguindo as regras do PostgreSQL. A região é validada quanto ao formato, sem comprovar a localização da audiência.

Ao atualizar uma VM com `0002` já aplicada, faça backup e execute `alembic upgrade head` (ou reconstrua a API pelo Compose). A migração falha se houver registros antigos sem proveniência completa ou com região incompatível; ela não inventa valores nem apaga dados para satisfazer as regras.

Para concluir a Fase 2 falta o cadastro de candidatos reais (pela API ou por CSV). As tabelas de fornecedor, review e score são apenas estrutura para fases futuras.

## Importação CSV

O arquivo precisa estar em **UTF-8** (no Excel: "CSV UTF-8"), separado por vírgulas, com cabeçalho na linha 1. Há um modelo vazio em [`docs/import_template.csv`](docs/import_template.csv).

```bash
# Na VM, a partir da pasta do projeto. 1) simular (não grava nada):
docker compose exec -T api python -m product_intelligence.jobs.import_csv - \
    --name lote_2026-09-26.csv --dry-run < lote_2026-09-26.csv
# 2) importar de verdade:
docker compose exec -T api python -m product_intelligence.jobs.import_csv - \
    --name lote_2026-09-26.csv < lote_2026-09-26.csv
```

Saída: 0 = tudo aceito ou já existente; 2 = algumas linhas rejeitadas (as outras foram gravadas); 1 = arquivo recusado ou falha, sem dados de domínio importados; 3 = outra importação em andamento. Arquivos recusados durante o processamento geram um lote `failed` para auditoria. Falhas de leitura, limite de tamanho excedido no comando e banco indisponível não geram lote.

| Coluna | Obrigatória | Formato |
|---|---|---|
| `product_name` | sim | até 200 caracteres |
| `source_type` | sim | `tiktok_top_ads`, `tiktok_top_products`, `tiktok_video`, `supplier`, `other` |
| `source_url` | sim | `http(s)://…` |
| `observed_at` | sim | ISO 8601 **com fuso**: `2026-09-26T12:00:00Z` ou `2026-09-26T09:00:00-03:00` |
| `market_region` | não | 2 letras, ex. `US` |
| `region_basis` | não (padrão `unknown`) | `source_filter_us` (exige `market_region=US`), `declared_creator`, `language_only`, `unknown`, `other` |
| `views`, `likes`, `comments_count`, `shares` | não | inteiro só com dígitos (`120000`); **não** use `1.2K` nem `1,200` |
| `observed_price_usd` | não | `19.99` — ponto decimal, sem `$` |
| `product_id` | não | id de um candidato existente, para desfazer ambiguidade de nome |
| `product_category`, `relation`, `external_id`, `title`, `creator_handle`, `published_at`, `note` | não | `relation`: `shows_product` (padrão), `similar_product`, `supplier_listing`, `other` |

Regras:

- **Célula vazia = desconhecido** (`NULL`), nunca zero. Linhas totalmente vazias são ignoradas.
- **Idempotente:** reimportar o mesmo arquivo não duplica nada; as linhas aparecem como "duplicadas". Cada execução fica registrada em `import_batch`.
- **Uma observação por fonte e instante.** Mesma fonte + mesmo `observed_at` com os mesmos valores, incluindo `note`, é duplicada; com valores diferentes é rejeitada (observações são imutáveis). Para um novo snapshot, use outro `observed_at`. Relação candidato/fonte conflitante também é rejeitada e exige revisão manual.
- **Candidato:** reaproveitado só quando o nome é idêntico (ignorando maiúsculas e espaços repetidos). Nomes parecidos viram candidatos diferentes; se houver dois candidatos com o mesmo nome, informe `product_id`.
- **Fonte:** reaproveitada pela URL normalizada (domínio em minúsculas, sem âncoras simples nem `utm_*`/`fbclid`/`gclid`). Ordem e codificação dos parâmetros são preservadas, pois podem identificar recursos ou assinaturas diferentes. Rotas de aplicações (`#/…`, `#!/…`, `#?…`) e parâmetros do TikTok são mantidos. Metadados (`title` etc.) só preenchem campos vazios; nunca sobrescrevem. Se houver possível conflito com a normalização antiga, a linha exige revisão manual da URL original em vez de unir ou duplicar fontes automaticamente.
- **Erros por linha** ficam em `import_row_error` com o conteúdo original. A referência é a linha física onde o registro começa, incluindo linhas vazias e campos multilinha. Células com NUL são rejeitadas e guardadas como `{"encoding": "base64-utf8", "value": "…"}` em `raw_row`, pois JSONB não aceita esse caractere; a codificação permite recuperar o original.
- Cabeçalho com coluna desconhecida ou obrigatória ausente, arquivo fora de UTF-8 ou CSV malformado: o arquivo inteiro é recusado, sem candidatos/fontes/observações gravados; apenas o lote de falha fica registrado. O limite é 20 MB por arquivo e o comando limita a leitura antes de carregá-lo inteiro.

## API

Com a API no ar, a documentação interativa (para testar cada rota pelo navegador) fica em `http://127.0.0.1:8000/docs`. Na VM, acesse pelo túnel SSH descrito em [Segurança](#segurança).

| Rota | O que faz |
|---|---|
| `POST /candidates` · `GET /candidates?q=&status=` · `GET/PATCH/DELETE /candidates/{id}` | Cadastro de candidatos. `GET /candidates/{id}` traz as fontes associadas. |
| `POST /candidates/{id}/sources` | Associa uma fonte ao candidato: por `source_id` ou por `source_type` + `url` (cria a fonte se não existir). |
| `PATCH /candidates/{id}/sources/{source_id}` · `DELETE …` | Revisão manual da associação (`relation`, `reviewed: true/false`) ou desassociação. |
| `POST /sources` · `GET /sources?source_type=&candidate_id=` · `GET/PATCH/DELETE /sources/{id}` | Cadastro de fontes. `GET /sources/{id}` traz candidatos, total de observações e a mais recente. |
| `POST /sources/{id}/observations` · `GET /sources/{id}/observations` | Registra um snapshot manual; lista em ordem cronológica. |
| `GET /observations/{id}` | Consulta uma observação. Não existe edição nem exclusão. |

Listas aceitam `limit` (1–200, padrão 50) e `offset` e devolvem `{items, total, limit, offset}`.

**Mesmas regras da importação CSV** — toda gravação passa por `product_intelligence/domain.py`, e cada campo é validado pelos mesmos validadores de `ingest/rows.py`:

- datas como texto ISO 8601 **com fuso** (`"2026-09-26T09:00:00-03:00"`), gravadas e devolvidas em UTC; datas futuras são recusadas;
- contagens inteiras ≥ 0 (`120000` ou `"120000"`; `"1.2K"`, `1.5` e `true` são recusados); preço com até 2 casas (`"19.99"`); região com 2 letras (`"us"` vira `"US"`); `source_filter_us` exige `US`;
- campo ausente ou `null` = desconhecido, nunca zero; campos desconhecidos no JSON são recusados (422);
- URL normalizada da mesma forma; fonte já existente é devolvida (200) e só tem campos vazios completados; fonte gravada com a normalização antiga bloqueia o cadastro (409);
- nome de candidato idêntico a outro (ignorando maiúsculas e espaços) é recusado (409), para o CSV nunca ficar ambíguo; nomes parecidos são permitidos;
- observação no mesmo instante: valores iguais = devolve a existente (200); diferentes = 409;
- o `status` do candidato não é editável por aqui: ele mudará por review (Fase 4).

Códigos: 201 criado · 200 já existia/consultado · 204 apagado · 404 não encontrado · 405 operação não permitida (editar/apagar observação) · 409 conflito com dado gravado ou histórico que impede exclusão · 422 entrada inválida (`detail` lista os motivos).

Exemplo pelo terminal da VM:

```bash
curl -s -X POST http://127.0.0.1:8000/candidates -H 'Content-Type: application/json' \
  -d '{"name": "Pet hair remover", "category": "Pets"}'
curl -s -X POST http://127.0.0.1:8000/candidates/1/sources -H 'Content-Type: application/json' \
  -d '{"source_type": "tiktok_top_ads", "url": "https://..."}'
curl -s -X POST http://127.0.0.1:8000/sources/1/observations -H 'Content-Type: application/json' \
  -d '{"observed_at": "2026-09-26T09:00:00-03:00", "views": 120000, "market_region": "US", "region_basis": "source_filter_us"}'
```

## 4. Testes

Um comando, com PostgreSQL real (inclui o teste de migração em banco vazio):

```bash
docker compose --profile test run --rm --build tests
```

Os testes de migração e de integridade criam e depois apagam bancos separados `pi_test_<identificador aleatório>`; os dados principais não são tocados. A conta precisa de permissão `CREATEDB` (a conta inicial do container já possui).

Desenvolvimento local sem Docker (o teste de migração é pulado se `TEST_DATABASE_URL` não estiver definido):

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check .
```

## 5. Backup e restauração

```bash
sh scripts/backup.sh                                  # gera backups/backup_<UTC>_<id>.dump e mantém os 14 últimos
sh scripts/restore.sh backups/<arquivo>.dump          # testa a restauração num banco temporário e o remove
sh scripts/restore.sh backups/<arquivo>.dump --replace   # substitui o banco principal (pede confirmação)
```

Backup diário via cron no host:

```bash
crontab -e
15 3 * * * cd /opt/product-intelligence && mkdir -p backups && sh scripts/backup.sh >> backups/backup.log 2>&1
```

**Teste a restauração** (`restore.sh` sem `--replace`) antes de depender do histórico, e copie `backups/` para fora da VM periodicamente — um backup no mesmo disco não protege contra perda da VM.
Se a restauração com `--replace` falhar, a API permanece parada para permitir a recuperação do banco antes de retomar o serviço.

## 6. Parar e atualizar

```bash
docker compose stop              # para, mantém containers e dados
docker compose down              # remove containers, MANTÉM o volume pgdata
docker compose down -v           # ⚠ apaga também o volume do banco (todos os dados)

git pull && docker compose up --build -d     # atualizar; migrações rodam na subida
```

## Segurança

- A API escuta só em `127.0.0.1`; o PostgreSQL não publica porta no host.
- Para acesso remoto, prefira túnel SSH (`ssh -L 8000:127.0.0.1:8000 usuario@vm`). Antes de abrir qualquer porta pública, coloque autenticação e HTTPS (ex.: proxy reverso com TLS).
- A localização da VM (Brasil) não define a região dos dados; ela apenas executa o processamento.

## Próximas fases

Ver [ARCHITECTURE.md §8](ARCHITECTURE.md#8-fases-de-entrega). Resumo do que cada parte do código deve receber:

| Fase | Onde |
|---|---|
| 2 — Dados reais | ✅ esquema (`db/models.py`, `0002`, `0003`) · ✅ importação CSV (`ingest/`, `jobs/import_csv.py`) · ✅ API (`api/`) · pendente: cadastrar candidatos reais |
| 3 — Histórico | `analytics/` (variação entre snapshots, cobertura) e rotas de consulta |
| 4 — Triagem | `analytics/` (score versionado), reviews, ofertas de fornecedor |
| 5 — Interface | dashboard e conectores autorizados |
