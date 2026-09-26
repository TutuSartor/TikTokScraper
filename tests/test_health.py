from fastapi.testclient import TestClient

from product_intelligence import __version__
from product_intelligence.db.session import build_engine, get_engine


def test_health_ok_when_database_responds(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "database": "ok", "version": __version__}


def test_health_503_when_database_unreachable(app):
    # Porta 1 em localhost: conexão recusada imediatamente.
    broken = build_engine("postgresql+psycopg://x:x@127.0.0.1:1/none?connect_timeout=1")
    app.dependency_overrides[get_engine] = lambda: broken
    try:
        with TestClient(app) as c:
            resp = c.get("/health")
    finally:
        app.dependency_overrides.clear()
        broken.dispose()
    assert resp.status_code == 503
    assert resp.json()["status"] == "unavailable"
    assert resp.json()["database"] == "error"
