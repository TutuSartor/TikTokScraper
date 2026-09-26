"""Comandos reproduzíveis para tarefas periódicas (ARCHITECTURE.md §7).

- `import_csv.py` — importa um CSV (`python -m product_intelligence.jobs.import_csv`),
  com trava no PostgreSQL contra execução simultânea e resumo de linhas aceitas,
  duplicadas e rejeitadas.
"""
