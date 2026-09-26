"""Ambiente Alembic: usa DATABASE_URL da configuração da aplicação e os metadados de Base."""

import importlib
import importlib.util
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from product_intelligence.config import get_settings
from product_intelligence.db.base import Base
from product_intelligence.db.types import UTCDateTime

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Permite sobrescrever a URL programaticamente (ex.: testes); senão usa a configuração da app.
if not config.get_main_option("sqlalchemy.url"):
    url = get_settings().sqlalchemy_url.render_as_string(hide_password=False)
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))

# Registra modelos (Fase 2+) no metadata, se o módulo existir.
if importlib.util.find_spec("product_intelligence.db.models") is not None:
    importlib.import_module("product_intelligence.db.models")

target_metadata = Base.metadata


def render_item(type_, obj, autogen_context):
    # A validação de UTC é Python; no banco continua sendo TIMESTAMP WITH TIME ZONE.
    # Evita gerar referências ao tipo customizado sem import nas próximas revisões.
    if type_ == "type" and isinstance(obj, UTCDateTime):
        return "sa.DateTime(timezone=True)"
    return False


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata,
            compare_type=True, render_item=render_item,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
