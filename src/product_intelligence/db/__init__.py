"""Camada de banco: base declarativa, sessões, modelos e vocabulários.

- `base.py`    — Base declarativa com convenção de nomes de constraints.
- `session.py` — engine, `get_session()` para as rotas.
- `enums.py`   — vocabulários controlados (source_type, region_basis, ...).
- `models.py`  — tabelas do domínio (ARCHITECTURE.md §4) e auditoria de importação (§5).

Toda mudança de esquema vira uma revisão: `alembic revision --autogenerate -m "..."`,
revisada à mão. `tests/test_models.py` falha se modelos e migrações divergirem.
"""

from product_intelligence.db.base import Base

__all__ = ["Base"]
