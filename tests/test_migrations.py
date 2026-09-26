"""Migrações sobem (e descem) em um PostgreSQL vazio.

O teste cria um banco de nome aleatório no servidor de TEST_DATABASE_URL.
No Compose, TEST_POSTGRES habilita o uso das credenciais da aplicação.
Somente o banco criado pelo teste é removido ao final.
"""

import os
import shutil
from io import StringIO
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.operations import ops
from alembic.script import ScriptDirectory
from sqlalchemy import Column, MetaData, Table, create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from product_intelligence.config import Settings, get_settings
from product_intelligence.db.types import UTCDateTime

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


def test_integrity_upgrade_preserves_existing_observation(tmp_path):
    url = f"sqlite:///{(tmp_path / 'existing.db').as_posix()}"
    cfg = _alembic_config(url)
    command.upgrade(cfg, "0002")
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO source_item (id, source_type, external_url)"
                              " VALUES (1, 'other', 'https://example.com/item')"))
            conn.execute(text("INSERT INTO observation"
                              " (id, source_item_id, observed_at, capture_method, views)"
                              " VALUES (1, 1, '2026-09-26 12:00:00', 'manual', 10)"))
        command.upgrade(cfg, "head")
        with engine.connect() as conn:
            assert conn.execute(text("SELECT views, likes FROM observation")).one() == (10, None)
        with pytest.raises(IntegrityError, match="immutable"), engine.begin() as conn:
            conn.execute(text("UPDATE observation SET views = 20 WHERE id = 1"))
        command.downgrade(cfg, "0002")
        with engine.begin() as conn:
            conn.execute(text("UPDATE observation SET views = 20 WHERE id = 1"))
        command.upgrade(cfg, "head")
        with engine.connect() as conn:
            assert conn.execute(text("SELECT views FROM observation")).scalar_one() == 20
    finally:
        engine.dispose()


def test_autogenerate_renders_utc_dates_as_native_sql_type(tmp_path):
    migrations = tmp_path / "migrations"
    shutil.copytree(ROOT / "migrations", migrations)
    cfg = _alembic_config(f"sqlite:///{(tmp_path / 'autogen.db').as_posix()}")
    cfg.set_main_option("script_location", str(migrations))
    command.upgrade(cfg, "head")

    def add_table(context, revision, directives):
        table = Table("example", MetaData(), Column("observed_at", UTCDateTime()))
        directives[0].upgrade_ops.ops.append(ops.CreateTableOp.from_table(table))

    result = command.revision(
        cfg, message="utc date", autogenerate=True, process_revision_directives=add_table
    )
    source = Path(result.path).read_text(encoding="utf-8")
    assert "sa.DateTime(timezone=True)" in source
    assert "product_intelligence.db.types.UTCDateTime" not in source
