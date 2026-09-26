# Product Intelligence

Sistema pessoal para registrar candidatos a produto, suas fontes e evidências datadas, e apoiar a triagem para testes numa loja Shopify voltada aos EUA. O desenho completo está em [ARCHITECTURE.md](ARCHITECTURE.md).

**Estado atual: Fase 1 — Fundação.** Existem projeto Python, Docker Compose com PostgreSQL, FastAPI com `/health`, Alembic (migração baseline vazia), configuração por `.env`, testes e scripts de backup. Ainda **não** há tabelas de domínio, CRUD, importação CSV, coleta TikTok, dashboard nem score.

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
│   ├── api/                # routers — Fase 1: health.py
│   ├── db/                 # Base declarativa, engine/sessão — modelos entram na Fase 2
│   ├── ingest/             # Fase 2 (vazio)
│   ├── analytics/          # Fases 3–4 (vazio)
│   └── jobs/               # Fase 2+ (vazio)
├── migrations/             # Alembic — versions/0001_baseline.py
├── scripts/                # backup.sh / restore.sh
└── tests/                  # health e migrações
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

Convenções: modelos herdam de `product_intelligence.db.Base` e ficam em `src/product_intelligence/db/models/` (o `migrations/env.py` importa esse módulo automaticamente quando ele existir). Revise sempre o arquivo gerado antes de commitar. Deve existir um único *head* — um teste falha se houver dois.

## 4. Testes

Um comando, com PostgreSQL real (inclui o teste de migração em banco vazio):

```bash
docker compose --profile test run --rm --build tests
```

O teste de migração cria e depois apaga um banco separado `pi_test_<identificador aleatório>`; os dados principais não são tocados. A conta precisa de permissão `CREATEDB` (a conta inicial do container já possui).

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
| 2 — Dados reais | `db/models/`, nova migração `0002_…`, `api/` (CRUD), `ingest/` (CSV idempotente), `jobs/` |
| 3 — Histórico | `analytics/` (variação entre snapshots, cobertura) e rotas de consulta |
| 4 — Triagem | `analytics/` (score versionado), reviews, ofertas de fornecedor |
| 5 — Interface | dashboard e conectores autorizados |
