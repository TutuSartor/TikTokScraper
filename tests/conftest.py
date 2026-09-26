import os

# Garante que importar a app nos testes nunca tente conectar ao banco de produção/dev.
if os.environ.get("TEST_POSTGRES") != "1":
    os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("APP_ENV", "test")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from product_intelligence.db.session import build_engine, get_engine  # noqa: E402
from product_intelligence.main import create_app  # noqa: E402


@pytest.fixture
def app():
    return create_app()


@pytest.fixture
def client(app):
    """Cliente com o banco substituído por SQLite em memória (só para testes)."""
    engine = build_engine("sqlite+pysqlite:///:memory:")
    app.dependency_overrides[get_engine] = lambda: engine
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    engine.dispose()
