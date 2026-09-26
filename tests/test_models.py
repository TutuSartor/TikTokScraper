"""Integridade do esquema da Fase 2.

Cada teste roda num banco criado pela MIGRAÇÃO (não por create_all), para validar o que
de fato vai para a VM. Sempre em SQLite; também em PostgreSQL quando TEST_DATABASE_URL
ou TEST_POSTGRES=1 estiver definido (caso do `docker compose --profile test run tests`).
"""

import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Enum, create_engine, event, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from product_intelligence.config import get_settings
from product_intelligence.db.base import Base
from product_intelligence.db.enums import CaptureMethod, RegionBasis, SourceType
from product_intelligence.db.models import (
    ImportBatch,
    ImportRowError,
    Observation,
    ProductCandidate,
    ProductSource,
    Review,
    ScoreRun,
    SourceItem,
    SupplierOffer,
)

ROOT = Path(__file__).resolve().parents[1]
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
HAS_POSTGRES = bool(TEST_DATABASE_URL) or os.environ.get("TEST_POSTGRES") == "1"
T0 = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


def _upgrade(url: str) -> None:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    command.upgrade(cfg, "head")


def _sqlite_engine(tmp_path) -> Engine:
    url = f"sqlite:///{(tmp_path / 'models.db').as_posix()}"
    _upgrade(url)
    engine = create_engine(url)

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _):  # SQLite só aplica FOREIGN KEY com este PRAGMA
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    return engine


@pytest.fixture(params=["sqlite", pytest.param("postgresql", marks=pytest.mark.skipif(
    not HAS_POSTGRES, reason="PostgreSQL de teste não configurado"))])
def engine(request, tmp_path):
    if request.param == "sqlite":
        eng = _sqlite_engine(tmp_path)
        yield eng
        eng.dispose()
        return
    base = make_url(TEST_DATABASE_URL) if TEST_DATABASE_URL else get_settings().sqlalchemy_url
    db_name = f"pi_test_{uuid4().hex}"
    admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    url = base.set(database=db_name).render_as_string(hide_password=False)
    eng = None
    try:
        _upgrade(url)
        eng = create_engine(url)
        yield eng
    finally:
        if eng is not None:
            eng.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


def _source(session, url="https://example.com/ad/1", source_type=SourceType.TIKTOK_TOP_ADS):
    item = SourceItem(source_type=source_type, external_url=url)
    session.add(item)
    session.flush()
    return item


def _obs(item, observed_at=T0, **kw):
    kw.setdefault("capture_method", CaptureMethod.MANUAL)
    return Observation(source_item_id=item.id, observed_at=observed_at, **kw)


def _expect_integrity_error(session, obj=None, sql=None):
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            if obj is not None:
                session.add(obj)
            if sql is not None:
                session.execute(text(sql))
            session.flush()


# ---------- esquema ----------


def test_migration_matches_models(engine):
    """Modelos e migrações não podem divergir (esqueceu de gerar migração?)."""
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == [], f"Modelos diferem das migrações: {diff}"


def test_enum_checks_accept_every_enum_value(engine):
    """O CHECK congelado na migração contém todos os valores atuais de cada enum."""
    with engine.connect() as conn:
        for table in Base.metadata.sorted_tables:
            if engine.dialect.name == "sqlite":
                ddl = conn.execute(
                    text("SELECT sql FROM sqlite_master WHERE name = :t"), {"t": table.name}
                ).scalar_one()
            else:
                ddl = " ".join(conn.execute(text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid = CAST(:t AS regclass) AND contype = 'c'"
                ), {"t": table.name}).scalars())
            for column in table.columns:
                if isinstance(column.type, Enum):
                    for value in column.type.enums:
                        assert f"'{value}'" in ddl, (
                            f"{table.name}.{column.name}: '{value}' ausente do CHECK — "
                            "crie uma migração para o novo valor"
                        )


# ---------- source_item ----------


def test_source_url_unique_per_type(session):
    _source(session, "https://example.com/x", SourceType.TIKTOK_VIDEO)
    duplicate = SourceItem(source_type=SourceType.TIKTOK_VIDEO, external_url="https://example.com/x")
    _expect_integrity_error(session, duplicate)
    # Mesma URL com outro tipo é permitida.
    _source(session, "https://example.com/x", SourceType.OTHER)


def test_invalid_enum_rejected_by_database(session):
    _expect_integrity_error(
        session,
        sql="INSERT INTO source_item (source_type, external_url) VALUES ('instagram', 'https://x')",
    )


def test_blank_url_and_name_rejected(session):
    _expect_integrity_error(
        session, SourceItem(source_type=SourceType.OTHER, external_url="   ")
    )
    _expect_integrity_error(session, ProductCandidate(name="  "))


# ---------- observation ----------


def test_missing_metrics_stay_null_not_zero(session):
    item = _source(session)
    session.add(_obs(item, views=1000))
    session.commit()
    obs = session.scalars(select(Observation)).one()
    assert obs.views == 1000
    assert obs.likes is None and obs.comments_count is None and obs.shares is None
    assert obs.observed_price_usd is None
    assert obs.market_region is None
    assert obs.region_basis == RegionBasis.UNKNOWN


def test_duplicate_observation_rejected(session):
    item = _source(session)
    session.add(_obs(item, views=10))
    session.flush()
    _expect_integrity_error(session, _obs(item, views=20))
    session.add(_obs(item, observed_at=T0 + timedelta(days=1), views=20))
    session.flush()


@pytest.mark.parametrize(
    "field,value",
    [("views", -1), ("likes", -1), ("comments_count", -5), ("shares", -1),
     ("observed_price_usd", Decimal("-0.01"))],
)
def test_negative_values_rejected(session, field, value):
    item = _source(session)
    _expect_integrity_error(session, _obs(item, **{field: value}))


def test_market_region_must_be_iso2(session):
    item = _source(session)
    _expect_integrity_error(session, _obs(item, market_region="us"))
    session.add(_obs(item, market_region="US", region_basis=RegionBasis.SOURCE_FILTER_US))
    session.flush()


@pytest.mark.parametrize("region", ["12", "A1", "  ", "U!", ""])
def test_market_region_rejects_non_letters(session, region):
    item = _source(session)
    _expect_integrity_error(session, _obs(item, market_region=region))


@pytest.mark.parametrize("region", [None, "BR"])
def test_us_filter_requires_explicit_us_region(session, region):
    item = _source(session)
    _expect_integrity_error(
        session, _obs(item, market_region=region, region_basis=RegionBasis.SOURCE_FILTER_US)
    )


def test_csv_observation_requires_batch_and_manual_forbids_it(session):
    item = _source(session)
    _expect_integrity_error(session, _obs(item, capture_method=CaptureMethod.CSV))
    batch = ImportBatch(source_filename="a.csv", file_sha256="0" * 64, rows_total=1)
    session.add(batch)
    session.flush()
    _expect_integrity_error(
        session, _obs(item, capture_method=CaptureMethod.MANUAL, import_batch_id=batch.id)
    )
    session.add(_obs(item, capture_method=CaptureMethod.CSV, import_batch_id=batch.id,
                     import_row_number=2))
    session.flush()


@pytest.mark.parametrize("row_number", [None, -1, 0, 1])
def test_csv_requires_valid_data_row_number(session, row_number):
    item = _source(session)
    batch = ImportBatch(source_filename="a.csv", file_sha256="0" * 64, rows_total=1)
    session.add(batch)
    session.flush()
    _expect_integrity_error(
        session, _obs(item, capture_method=CaptureMethod.CSV,
                      import_batch_id=batch.id, import_row_number=row_number)
    )


def test_naive_observation_date_is_rejected(session):
    item = _source(session)
    with pytest.raises(StatementError, match="fuso horário"):
        with session.begin_nested():
            session.add(_obs(item, observed_at=T0.replace(tzinfo=None)))
            session.flush()


def test_offset_dates_roundtrip_in_utc_and_deduplicate(session):
    item = _source(session)
    offset_date = datetime.fromisoformat("2026-09-26T09:00:00-03:00")
    session.add(_obs(item, observed_at=offset_date))
    session.commit()
    obs = session.scalars(select(Observation)).one()
    assert obs.observed_at == T0
    assert obs.observed_at.tzinfo == UTC
    _expect_integrity_error(session, _obs(item, observed_at=T0))


@pytest.mark.parametrize("operation", ["update", "delete", "sql_update", "sql_delete"])
def test_observation_is_immutable(session, operation):
    item = _source(session)
    obs = _obs(item, views=10)
    session.add(obs)
    session.commit()
    with pytest.raises(IntegrityError, match="immutable"):
        with session.begin_nested():
            if operation == "update":
                obs.views = 20
            elif operation == "delete":
                session.delete(obs)
            elif operation == "sql_update":
                session.execute(text("UPDATE observation SET views = 20 WHERE id = :id"),
                                {"id": obs.id})
            else:
                session.execute(text("DELETE FROM observation WHERE id = :id"), {"id": obs.id})
            session.flush()
    session.expire_all()
    assert session.get(Observation, obs.id).views == 10


@pytest.mark.parametrize("loaded", [False, True])
def test_orm_delete_product_cascades_associations(session, loaded):
    item = _source(session)
    product = ProductCandidate(name="P")
    session.add(product)
    session.flush()
    session.add(ProductSource(product_id=product.id, source_item_id=item.id))
    session.commit()
    if loaded:
        assert len(product.sources) == 1
    session.delete(product)
    session.commit()
    assert session.scalar(text("SELECT count(*) FROM product_source")) == 0
    assert session.get(SourceItem, item.id) is not None


@pytest.mark.parametrize("loaded", [False, True])
def test_orm_delete_source_cascades_associations(session, loaded):
    item = _source(session)
    product = ProductCandidate(name="P")
    session.add(product)
    session.flush()
    session.add(ProductSource(product_id=product.id, source_item_id=item.id))
    session.commit()
    if loaded:
        assert len(item.products) == 1
    session.delete(item)
    session.commit()
    assert session.scalar(text("SELECT count(*) FROM product_source")) == 0
    assert session.get(ProductCandidate, product.id) is not None


def test_orm_delete_source_with_loaded_observations_is_restricted(session):
    item = _source(session)
    session.add(_obs(item, views=10))
    session.commit()
    assert len(item.observations) == 1
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.delete(item)
            session.flush()
    session.expire_all()
    assert session.scalars(select(Observation)).one().source_item_id == item.id


def test_orm_delete_product_with_loaded_reviews_is_restricted(session):
    product = ProductCandidate(name="P")
    session.add(product)
    session.flush()
    session.add(Review(product_id=product.id, decision="watch", reason="evidência inicial"))
    session.commit()
    assert len(product.reviews) == 1
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.delete(product)
            session.flush()
    session.expire_all()
    assert session.scalars(select(Review)).one().product_id == product.id


# ---------- apagar histórico ----------


def test_source_with_observations_cannot_be_deleted(session):
    item = _source(session)
    session.add(_obs(item))
    session.commit()
    _expect_integrity_error(session, sql=f"DELETE FROM source_item WHERE id = {item.id}")


def test_deleting_product_cascades_links_but_not_history(session):
    product = ProductCandidate(name="Pet hair remover")
    item = _source(session)
    session.add(product)
    session.flush()
    session.add(ProductSource(product_id=product.id, source_item_id=item.id))
    session.commit()

    # Sem histórico próprio: apagar o produto remove só a associação; o item de fonte fica.
    session.execute(text(f"DELETE FROM product_candidate WHERE id = {product.id}"))
    session.commit()
    session.expunge_all()
    assert session.scalar(text("SELECT count(*) FROM product_source")) == 0
    assert session.scalar(text("SELECT count(*) FROM source_item")) == 1

    # Com review registrada, apagar o produto é bloqueado.
    other = ProductCandidate(name="Outro")
    session.add(other)
    session.flush()
    session.add(Review(product_id=other.id, decision="watch", reason="volume crescendo"))
    session.commit()
    _expect_integrity_error(session, sql=f"DELETE FROM product_candidate WHERE id = {other.id}")


# ---------- fornecedor, score, importação ----------


def test_supplier_conversion_must_be_documented(session):
    product = ProductCandidate(name="P")
    session.add(product)
    session.flush()
    base = {"product_id": product.id, "supplier_url": "https://s.example", "observed_at": T0}
    _expect_integrity_error(
        session, SupplierOffer(**base, unit_cost_usd=Decimal("2.50"), original_currency="CNY",
                               original_unit_cost=Decimal("18.00"))
    )
    _expect_integrity_error(
        session, SupplierOffer(**base, original_unit_cost=Decimal("18.00"))
    )
    session.add(SupplierOffer(
        **base, unit_cost_usd=Decimal("2.50"), original_currency="CNY",
        original_unit_cost=Decimal("18.00"), fx_rate_to_usd=Decimal("0.13888889"),
        fx_rate_date=date(2026, 9, 26),
    ))
    session.add(SupplierOffer(**base, unit_cost_usd=Decimal("3.00")))  # cotado direto em USD
    session.flush()


def test_score_coverage_bounds_and_null_score(session):
    product = ProductCandidate(name="P")
    session.add(product)
    session.flush()
    _expect_integrity_error(session, ScoreRun(
        product_id=product.id, rule_version="v1", components_json={}, coverage=Decimal("1.5")
    ))
    session.add(ScoreRun(product_id=product.id, rule_version="v1",
                         components_json={"growth": None}, coverage=Decimal("0"), score=None))
    session.flush()


def test_import_batch_counts_and_row_errors(session):
    _expect_integrity_error(session, ImportBatch(
        source_filename="a.csv", file_sha256="0" * 64, rows_total=1, rows_accepted=1,
        rows_rejected=1,
    ))
    batch = ImportBatch(source_filename="a.csv", file_sha256="0" * 64, rows_total=2,
                        rows_accepted=1, rows_rejected=1)
    batch.errors.append(ImportRowError(row_number=3, error="views negativo",
                                       raw_row={"views": "-1"}))
    session.add(batch)
    session.commit()
    err = session.scalars(select(ImportRowError)).one()
    assert err.raw_row == {"views": "-1"}
    assert batch.status == "running"
