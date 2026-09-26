"""Camada de banco: base declarativa, sessões e (a partir da Fase 2) modelos e repositórios.

Convenção para a Fase 2:
- Modelos em `db/models.py` (ou `db/models/<entidade>.py`), herdando de `Base`.
- Importar os modelos em `db/models/__init__.py` para que o Alembic os enxergue
  (migrations/env.py importa `product_intelligence.db.models` se existir).
- Toda mudança de esquema vira uma revisão: `alembic revision --autogenerate -m "..."`.
"""

from product_intelligence.db.base import Base

__all__ = ["Base"]
