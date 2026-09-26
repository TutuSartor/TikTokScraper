"""Rotas HTTP.

- health.py     — GET /health
- candidates.py — /candidates (CRUD) e /candidates/{id}/sources (associações)
- sources.py    — /sources (fontes), /sources/{id}/observations e /observations/{id}

Toda escrita passa por `product_intelligence.domain`, as mesmas regras da importação CSV.
"""

from fastapi import APIRouter

from product_intelligence.api.candidates import router as candidates_router
from product_intelligence.api.health import router as health_router
from product_intelligence.api.sources import router as sources_router

api_router = APIRouter()
api_router.include_router(health_router)
api_router.include_router(candidates_router)
api_router.include_router(sources_router)
