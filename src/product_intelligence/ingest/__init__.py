"""Importação de dados — Fase 2 (vazio na Fase 1).

Escopo previsto (ARCHITECTURE.md §5): CSV UTF-8 com cabeçalho documentado, validação de
enums/USD/datas UTC, normalização de URL, upsert idempotente de source_item, observações sem
duplicatas, erros registrados por linha e referência ao lote de importação.
Regra central: métrica ausente é NULL, nunca zero.
"""
