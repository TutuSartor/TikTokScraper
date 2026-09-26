"""Importação de dados (ARCHITECTURE.md §5).

- `urls.py`     — normalização conservadora de URLs de fonte.
- `rows.py`     — colunas aceitas e validação de cada linha (vazio = NULL, datas com fuso).
- `importer.py` — `import_csv()`: lote auditável, upsert de fonte, observações sem
  duplicatas, erros por linha, tudo numa transação.

O comando de linha de comando fica em `product_intelligence.jobs.import_csv`.
"""
