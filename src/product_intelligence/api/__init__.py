"""Rotas HTTP.

Fase 1: apenas /health.
Fase 2+: routers de candidatos, itens de fonte, observações e shortlist devem ser
criados em módulos próprios (ex.: api/candidates.py) e incluídos em `api_router` abaixo.
"""

from fastapi import APIRouter

from product_intelligence.api.health import router as health_router

api_router = APIRouter()
api_router.include_router(health_router)
