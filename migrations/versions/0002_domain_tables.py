"""Tabelas do domínio (ARCHITECTURE.md §4) e auditoria de importação (§5).

Cria product_candidate, source_item, product_source, observation, supplier_offer,
review, score_run, import_batch e import_row_error.

Os valores dos enums estão congelados aqui de propósito: acrescentar um valor em
product_intelligence/db/enums.py exige uma nova migração que recrie o CHECK.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-26
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0002'
down_revision: str | None = '0001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('import_batch',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('source_filename', sa.String(length=255), nullable=False),
    sa.Column('file_sha256', sa.String(length=64), nullable=False),
    sa.Column('status', sa.Enum('running', 'completed', 'failed', name='import_status', native_enum=False, create_constraint=True, length=32), server_default='running', nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('rows_total', sa.Integer(), server_default='0', nullable=False),
    sa.Column('rows_accepted', sa.Integer(), server_default='0', nullable=False),
    sa.Column('rows_rejected', sa.Integer(), server_default='0', nullable=False),
    sa.CheckConstraint('rows_total >= 0 AND rows_accepted >= 0 AND rows_rejected >= 0 AND rows_accepted + rows_rejected <= rows_total', name=op.f('ck_import_batch_row_counts_consistent')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_import_batch'))
    )
    op.create_index('ix_import_batch_file_sha256', 'import_batch', ['file_sha256'], unique=False)
    op.create_table('product_candidate',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('category', sa.String(length=100), nullable=True),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('status', sa.Enum('new', 'watch', 'research_supplier', 'test', 'reject', name='candidate_status', native_enum=False, create_constraint=True, length=32), server_default='new', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.CheckConstraint('length(trim(name)) > 0', name=op.f('ck_product_candidate_name_not_blank')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_product_candidate'))
    )
    op.create_index('ix_product_candidate_status', 'product_candidate', ['status'], unique=False)
    op.create_table('source_item',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('source_type', sa.Enum('tiktok_top_ads', 'tiktok_top_products', 'tiktok_video', 'supplier', 'other', name='source_type', native_enum=False, create_constraint=True, length=32), nullable=False),
    sa.Column('external_url', sa.String(length=2048), nullable=False),
    sa.Column('external_id', sa.String(length=255), nullable=True),
    sa.Column('title', sa.String(length=500), nullable=True),
    sa.Column('creator_handle', sa.String(length=255), nullable=True),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.CheckConstraint('length(trim(external_url)) > 0', name=op.f('ck_source_item_external_url_not_blank')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_source_item')),
    sa.UniqueConstraint('source_type', 'external_url', name=op.f('uq_source_item_source_type_external_url'))
    )
    op.create_table('import_row_error',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('batch_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('row_number', sa.Integer(), nullable=False),
    sa.Column('error', sa.Text(), nullable=False),
    sa.Column('raw_row', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.CheckConstraint('row_number >= 1', name=op.f('ck_import_row_error_row_number_positive')),
    sa.ForeignKeyConstraint(['batch_id'], ['import_batch.id'], name=op.f('fk_import_row_error_batch_id_import_batch'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_import_row_error'))
    )
    op.create_index('ix_import_row_error_batch_id', 'import_row_error', ['batch_id'], unique=False)
    op.create_table('observation',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('source_item_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('market_region', sa.String(length=2), nullable=True),
    sa.Column('region_basis', sa.Enum('source_filter_us', 'declared_creator', 'language_only', 'unknown', 'other', name='region_basis', native_enum=False, create_constraint=True, length=32), server_default='unknown', nullable=False),
    sa.Column('views', sa.BigInteger(), nullable=True),
    sa.Column('likes', sa.BigInteger(), nullable=True),
    sa.Column('comments_count', sa.BigInteger(), nullable=True),
    sa.Column('shares', sa.BigInteger(), nullable=True),
    sa.Column('observed_price_usd', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('capture_method', sa.Enum('manual', 'csv', 'approved_api', name='capture_method', native_enum=False, create_constraint=True, length=32), nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('import_batch_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=True),
    sa.Column('import_row_number', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.CheckConstraint("(capture_method = 'csv') = (import_batch_id IS NOT NULL)", name=op.f('ck_observation_csv_requires_batch')),
    sa.CheckConstraint('(views IS NULL OR views >= 0) AND (likes IS NULL OR likes >= 0) AND (comments_count IS NULL OR comments_count >= 0) AND (shares IS NULL OR shares >= 0)', name=op.f('ck_observation_counts_non_negative')),
    sa.CheckConstraint('import_row_number IS NULL OR import_batch_id IS NOT NULL', name=op.f('ck_observation_row_number_requires_batch')),
    sa.CheckConstraint('market_region IS NULL OR (length(market_region) = 2 AND market_region = upper(market_region))', name=op.f('ck_observation_market_region_iso2')),
    sa.CheckConstraint('observed_price_usd IS NULL OR observed_price_usd >= 0', name=op.f('ck_observation_price_non_negative')),
    sa.ForeignKeyConstraint(['import_batch_id'], ['import_batch.id'], name=op.f('fk_observation_import_batch_id_import_batch'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['source_item_id'], ['source_item.id'], name=op.f('fk_observation_source_item_id_source_item'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_observation')),
    sa.UniqueConstraint('source_item_id', 'observed_at', name=op.f('uq_observation_source_item_id_observed_at'))
    )
    op.create_index('ix_observation_import_batch_id', 'observation', ['import_batch_id'], unique=False)
    op.create_index('ix_observation_observed_at', 'observation', ['observed_at'], unique=False)
    op.create_table('product_source',
    sa.Column('product_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('source_item_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('relation', sa.Enum('shows_product', 'similar_product', 'supplier_listing', 'other', name='source_relation', native_enum=False, create_constraint=True, length=32), server_default='shows_product', nullable=False),
    sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['product_id'], ['product_candidate.id'], name=op.f('fk_product_source_product_id_product_candidate'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['source_item_id'], ['source_item.id'], name=op.f('fk_product_source_source_item_id_source_item'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('product_id', 'source_item_id', name=op.f('pk_product_source'))
    )
    op.create_index('ix_product_source_source_item_id', 'product_source', ['source_item_id'], unique=False)
    op.create_table('review',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('product_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('decision', sa.Enum('watch', 'research_supplier', 'test', 'reject', name='review_decision', native_enum=False, create_constraint=True, length=32), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('reviewed_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.CheckConstraint('length(trim(reason)) > 0', name=op.f('ck_review_reason_not_blank')),
    sa.ForeignKeyConstraint(['product_id'], ['product_candidate.id'], name=op.f('fk_review_product_id_product_candidate'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_review'))
    )
    op.create_index('ix_review_product_id_reviewed_at', 'review', ['product_id', 'reviewed_at'], unique=False)
    op.create_table('score_run',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('product_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('rule_version', sa.String(length=50), nullable=False),
    sa.Column('computed_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('components_json', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('coverage', sa.Numeric(precision=5, scale=4), nullable=False),
    sa.Column('score', sa.Numeric(precision=10, scale=4), nullable=True),
    sa.CheckConstraint('coverage >= 0 AND coverage <= 1', name=op.f('ck_score_run_coverage_fraction')),
    sa.ForeignKeyConstraint(['product_id'], ['product_candidate.id'], name=op.f('fk_score_run_product_id_product_candidate'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_score_run'))
    )
    op.create_index('ix_score_run_product_id_computed_at', 'score_run', ['product_id', 'computed_at'], unique=False)
    op.create_table('supplier_offer',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('product_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('supplier_url', sa.String(length=2048), nullable=False),
    sa.Column('unit_cost_usd', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('shipping_usd', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('original_currency', sa.String(length=3), nullable=True),
    sa.Column('original_unit_cost', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('original_shipping', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('fx_rate_to_usd', sa.Numeric(precision=18, scale=8), nullable=True),
    sa.Column('fx_rate_date', sa.Date(), nullable=True),
    sa.Column('delivery_days', sa.Integer(), nullable=True),
    sa.Column('ships_from', sa.String(length=100), nullable=True),
    sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('verification_status', sa.Enum('unverified', 'supplier_quoted', 'verified', name='verification_status', native_enum=False, create_constraint=True, length=32), server_default='unverified', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.CheckConstraint('(original_unit_cost IS NULL AND original_shipping IS NULL) OR original_currency IS NOT NULL', name=op.f('ck_supplier_offer_original_amounts_need_currency')),
    sa.CheckConstraint('(unit_cost_usd IS NULL OR unit_cost_usd >= 0) AND (shipping_usd IS NULL OR shipping_usd >= 0) AND (original_unit_cost IS NULL OR original_unit_cost >= 0) AND (original_shipping IS NULL OR original_shipping >= 0)', name=op.f('ck_supplier_offer_amounts_non_negative')),
    sa.CheckConstraint('delivery_days IS NULL OR delivery_days >= 0', name=op.f('ck_supplier_offer_delivery_days_ok')),
    sa.CheckConstraint('original_currency IS NULL OR (length(original_currency) = 3 AND fx_rate_to_usd IS NOT NULL AND fx_rate_to_usd > 0 AND fx_rate_date IS NOT NULL)', name=op.f('ck_supplier_offer_conversion_documented')),
    sa.ForeignKeyConstraint(['product_id'], ['product_candidate.id'], name=op.f('fk_supplier_offer_product_id_product_candidate'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_supplier_offer'))
    )
    op.create_index('ix_supplier_offer_product_id', 'supplier_offer', ['product_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_supplier_offer_product_id', table_name='supplier_offer')
    op.drop_table('supplier_offer')
    op.drop_index('ix_score_run_product_id_computed_at', table_name='score_run')
    op.drop_table('score_run')
    op.drop_index('ix_review_product_id_reviewed_at', table_name='review')
    op.drop_table('review')
    op.drop_index('ix_product_source_source_item_id', table_name='product_source')
    op.drop_table('product_source')
    op.drop_index('ix_observation_observed_at', table_name='observation')
    op.drop_index('ix_observation_import_batch_id', table_name='observation')
    op.drop_table('observation')
    op.drop_index('ix_import_row_error_batch_id', table_name='import_row_error')
    op.drop_table('import_row_error')
    op.drop_table('source_item')
    op.drop_index('ix_product_candidate_status', table_name='product_candidate')
    op.drop_table('product_candidate')
    op.drop_index('ix_import_batch_file_sha256', table_name='import_batch')
    op.drop_table('import_batch')
