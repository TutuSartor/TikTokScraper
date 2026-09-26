"""Ponto de entrada da API (uvicorn product_intelligence.main:app)."""

import logging

from fastapi import FastAPI

from product_intelligence import __version__
from product_intelligence.api import api_router
from product_intelligence.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app = FastAPI(
        title="Product Intelligence",
        version=__version__,
        description="Registro de candidatos a produto e evidências datadas. Fase 1: fundação.",
    )
    app.include_router(api_router)
    return app


app = create_app()
