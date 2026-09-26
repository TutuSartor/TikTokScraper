"""Healthcheck: responde 200 somente se o banco aceitar uma consulta."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.engine import Engine

from product_intelligence import __version__
from product_intelligence.db.session import check_database, get_engine

logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])


@router.get("/health")
def health(engine: Annotated[Engine, Depends(get_engine)]) -> JSONResponse:
    try:
        check_database(engine)
    except Exception as exc:  # noqa: BLE001 — qualquer falha de conexão vira 503
        logger.warning("Healthcheck falhou: %s", exc.__class__.__name__)
        return JSONResponse(
            status_code=503,
            content={"status": "unavailable", "database": "error", "version": __version__},
        )
    return JSONResponse(
        status_code=200,
        content={"status": "ok", "database": "ok", "version": __version__},
    )
