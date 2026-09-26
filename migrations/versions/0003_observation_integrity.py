"""Proveniência completa e snapshots imutáveis.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REGION_OLD = (
    "market_region IS NULL OR (length(market_region) = 2"
    " AND market_region = upper(market_region))"
)
REGION_NEW = (
    "market_region IS NULL OR (length(market_region) = 2"
    " AND market_region = upper(market_region)"
    " AND substr(market_region, 1, 1) BETWEEN 'A' AND 'Z'"
    " AND substr(market_region, 2, 1) BETWEEN 'A' AND 'Z')"
)
ROW_OLD = "import_row_number IS NULL OR import_batch_id IS NOT NULL"
ROW_NEW = (
    "(import_batch_id IS NULL AND import_row_number IS NULL) OR"
    " (import_batch_id IS NOT NULL AND import_row_number IS NOT NULL"
    " AND import_row_number >= 2)"
)
US_REGION = (
    "region_basis <> 'source_filter_us' OR"
    " (market_region IS NOT NULL AND market_region = 'US')"
)


def _replace_checks(upgrading: bool) -> None:
    with op.batch_alter_table("observation") as batch:
        batch.drop_constraint(op.f("ck_observation_market_region_iso2"), type_="check")
        batch.drop_constraint(op.f("ck_observation_row_number_requires_batch"), type_="check")
        batch.create_check_constraint(
            op.f("ck_observation_market_region_iso2"), REGION_NEW if upgrading else REGION_OLD
        )
        batch.create_check_constraint(
            op.f("ck_observation_row_number_requires_batch"), ROW_NEW if upgrading else ROW_OLD
        )
        if upgrading:
            batch.create_check_constraint(op.f("ck_observation_us_filter_requires_us_region"), US_REGION)
        else:
            batch.drop_constraint(op.f("ck_observation_us_filter_requires_us_region"), type_="check")


def upgrade() -> None:
    # Dados incompatíveis fazem a migração falhar: não inventar região ou linha de origem.
    _replace_checks(upgrading=True)
    if op.get_bind().dialect.name == "postgresql":
        op.execute("""
            CREATE FUNCTION reject_observation_mutation() RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'Observations are immutable'
                    USING ERRCODE = '23514';
            END;
            $$ LANGUAGE plpgsql
        """)
        op.execute("""
            CREATE TRIGGER observation_immutable
            BEFORE UPDATE OR DELETE ON observation
            FOR EACH ROW EXECUTE FUNCTION reject_observation_mutation()
        """)
    else:
        for operation in ("UPDATE", "DELETE"):
            op.execute(f"""
                CREATE TRIGGER observation_no_{operation.lower()}
                BEFORE {operation} ON observation
                BEGIN
                    SELECT RAISE(ABORT, 'Observations are immutable');
                END
            """)


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER observation_immutable ON observation")
        op.execute("DROP FUNCTION reject_observation_mutation()")
    else:
        op.execute("DROP TRIGGER observation_no_update")
        op.execute("DROP TRIGGER observation_no_delete")
    _replace_checks(upgrading=False)
