"""Engine e sessões do banco."""

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from product_intelligence.config import get_settings


def build_engine(database_url: str | URL) -> Engine:
    url = make_url(database_url)
    # O prazo de conexão deve caber no healthcheck do Compose (5 segundos).
    connect_args = {"connect_timeout": 3} if url.get_backend_name() == "postgresql" else {}
    return create_engine(url, pool_pre_ping=True, connect_args=connect_args)


@lru_cache
def get_engine() -> Engine:
    return build_engine(get_settings().sqlalchemy_url)


def check_database(engine: Engine) -> None:
    """Executa SELECT 1; levanta exceção se o banco não responder."""
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))


def get_session() -> Iterator[Session]:
    """Dependência FastAPI para as rotas da Fase 2+: uma sessão por requisição."""
    session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
