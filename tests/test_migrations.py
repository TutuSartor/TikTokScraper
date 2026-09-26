"""Migrações sobem (e descem) em um PostgreSQL vazio.

O teste cria um banco de nome aleatório no servidor de TEST_DATABASE_URL.
No Compose, TEST_POSTGRES habilita o uso das credenciais da aplicação.
Somente o banco criado pelo teste é removido ao final.
"""

import os
from io import StringIO
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from product_intelligence.config import Settings, get_settings

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
TEST_POSTGRES = os.environ.get("TEST_POSTGRES") == "1"
ROOT = Path(__file__).resolve().parents[1]

requires_postgres = pytest.mark.skipif(
    not (TEST_DATABASE_URL or TEST_POSTGRES), reason="PostgreSQL de teste não configurado"
)


@pytest.fixture
def empty_postgres():
    url = make_url(TEST_DATABASE_URL) if TEST_DATABASE_URL else get_settings().sqlalchemy_url
    assert url.get_backend_name() == "postgresql", "TEST_DATABASE_URL deve ser PostgreSQL"
    db_name = f"pi_test_{uuid4().hex}"
    url = url.set(database=db_name)
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    created = False
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{db_name}"'))
        created = True
        yield url.render_as_string(hide_password=False)
    finally:
        try:
            if created:
                with admin.connect() as conn:
                    conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))
        finally:
            admin.dispose()


def _alembic_config(db_url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", db_url.replace("%", "%%"))
    return cfg


def _current_revision(db_url: str) -> str | None:
    engine = create_engine(db_url)
    try:
        with engine.connect() as conn:
            if not inspect(conn).has_table("alembic_version"):
                return None
            return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    finally:
        engine.dispose()


@requires_postgres
def test_upgrade_downgrade_upgrade_on_empty_database(empty_postgres):
    cfg = _alembic_config(empty_postgres)
    head = ScriptDirectory.from_config(cfg).get_current_head()

    command.upgrade(cfg, "head")
    assert _current_revision(empty_postgres) == head

    command.downgrade(cfg, "base")
    assert _current_revision(empty_postgres) is None

    command.upgrade(cfg, "head")
    assert _current_revision(empty_postgres) == head


def test_single_migration_head():
    cfg = _alembic_config("sqlite://")
    heads = ScriptDirectory.from_config(cfg).get_heads()
    assert len(heads) == 1, f"Mais de um head de migração: {heads} — faça merge das revisões"


def test_migrations_use_settings_with_special_password(monkeypatch):
    settings = Settings(
        _env_file=None, database_url=None, postgres_password="p@ss:%word/#?"
    )
    monkeypatch.setattr("product_intelligence.config.get_settings", lambda: settings)
    output = StringIO()
    cfg = Config(str(ROOT / "alembic.ini"), output_buffer=output)
    command.upgrade(cfg, "head", sql=True)
    assert "INSERT INTO alembic_version" in output.getvalue()
    assert make_url(cfg.get_main_option("sqlalchemy.url")).password == settings.postgres_password


def test_migration_roundtrip_locally(tmp_path):
    url = f"sqlite:///{(tmp_path / 'migration.db').as_posix()}"
    cfg = _alembic_config(url)
    head = ScriptDirectory.from_config(cfg).get_current_head()
    command.upgrade(cfg, "head")
    assert _current_revision(url) == head
    command.downgrade(cfg, "base")
    assert _current_revision(url) is None
    command.upgrade(cfg, "head")
    assert _current_revision(url) == head
