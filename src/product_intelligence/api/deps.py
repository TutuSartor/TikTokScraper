"""Dependências compartilhadas das rotas."""

from collections.abc import Iterator
from typing import Annotated, NoReturn

from fastapi import Depends, HTTPException, Query
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from product_intelligence.db.session import get_engine
from product_intelligence.domain import ConflictError, DomainError

MAX_PAGE_SIZE = 200


def get_db(engine: Annotated[Engine, Depends(get_engine)]) -> Iterator[Session]:
    """Uma sessão por requisição; cada rota de escrita faz o próprio commit."""
    with Session(engine, expire_on_commit=False) as session:
        yield session


DbSession = Annotated[Session, Depends(get_db)]


class Pagination:
    def __init__(
        self,
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ):
        self.limit = limit
        self.offset = offset


Paging = Annotated[Pagination, Depends()]


def not_found(what: str, ident: int) -> NoReturn:
    raise HTTPException(status_code=404, detail=[f"{what} {ident} não encontrado"])


def domain_http_error(session: Session, exc: DomainError) -> NoReturn:
    """Desfaz a transação e converte a regra violada em 409 (conflito) ou 422."""
    session.rollback()
    status = 409 if isinstance(exc, ConflictError) else 422
    raise HTTPException(status_code=status, detail=exc.messages) from None
