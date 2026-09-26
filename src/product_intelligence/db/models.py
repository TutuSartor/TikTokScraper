"""Modelos do domínio (ARCHITECTURE.md §4 e §5).

Regras que o esquema garante por si só:
- Métrica desconhecida é NULL, nunca zero; contagens e valores monetários não podem ser negativos.
- `source_item` é único por (source_type, external_url).
- `observation` é único por (source_item_id, observed_at): a mesma coleta não entra duas vezes.
- Observações, reviews, cotações e score_runs são histórico: apagar o produto/fonte que
  as referencia é bloqueado (RESTRICT). Só a associação `product_source` é apagada em cascata.
- Valor convertido para USD exige moeda original, taxa e data da taxa (§4, último parágrafo).

Toda mudança aqui precisa de uma migração em migrations/versions/.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    Date,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from product_intelligence.db.base import Base
from product_intelligence.db.enums import (
    CandidateStatus,
    CaptureMethod,
    ImportStatus,
    RegionBasis,
    ReviewDecision,
    SourceRelation,
    SourceType,
    VerificationStatus,
)
from product_intelligence.db.types import UTCDateTime

# BIGINT no PostgreSQL; INTEGER no SQLite (necessário para autoincremento lá).
BigId = BigInteger().with_variant(Integer(), "sqlite")
Json = JSON().with_variant(JSONB(), "postgresql")
Money = Numeric(12, 2)
Count = BigInteger()


def enum_column(enum_cls: type[StrEnum], name: str) -> Enum:
    """Texto + CHECK com os valores do enum (portável entre PostgreSQL e SQLite)."""
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=32,
        validate_strings=True,
        values_callable=lambda cls: [member.value for member in cls],
    )


def created_at_column() -> Mapped[datetime]:
    return mapped_column(UTCDateTime(), nullable=False, server_default=func.now())


class ProductCandidate(Base):
    """Um conceito de produto. Nomes parecidos não são unidos automaticamente."""

    __tablename__ = "product_candidate"

    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str | None] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[CandidateStatus] = mapped_column(
        enum_column(CandidateStatus, "candidate_status"),
        nullable=False,
        default=CandidateStatus.NEW,
        server_default=CandidateStatus.NEW.value,
    )
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    sources: Mapped[list[ProductSource]] = relationship(
        back_populates="product", passive_deletes="all"
    )
    supplier_offers: Mapped[list[SupplierOffer]] = relationship(
        back_populates="product", passive_deletes="all"
    )
    reviews: Mapped[list[Review]] = relationship(back_populates="product", passive_deletes="all")

    __table_args__ = (
        CheckConstraint("length(trim(name)) > 0", name="name_not_blank"),
        Index("ix_product_candidate_status", "status"),
    )


class SourceItem(Base):
    """Um vídeo, anúncio, página de tendência ou link de fornecedor."""

    __tablename__ = "source_item"

    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    source_type: Mapped[SourceType] = mapped_column(
        enum_column(SourceType, "source_type"), nullable=False
    )
    # URL já normalizada pela camada de ingestão (sem remover identificadores necessários).
    external_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(255))
    title: Mapped[str | None] = mapped_column(String(500))
    creator_handle: Mapped[str | None] = mapped_column(String(255))
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = created_at_column()

    products: Mapped[list[ProductSource]] = relationship(
        back_populates="source_item", passive_deletes="all"
    )
    observations: Mapped[list[Observation]] = relationship(
        back_populates="source_item", passive_deletes="all"
    )

    __table_args__ = (
        UniqueConstraint("source_type", "external_url"),
        CheckConstraint("length(trim(external_url)) > 0", name="external_url_not_blank"),
    )


class ProductSource(Base):
    """Associação muitos-para-muitos candidato ↔ item de fonte, revisável manualmente."""

    __tablename__ = "product_source"

    product_id: Mapped[int] = mapped_column(
        BigId, ForeignKey("product_candidate.id", ondelete="CASCADE"), primary_key=True
    )
    source_item_id: Mapped[int] = mapped_column(
        BigId, ForeignKey("source_item.id", ondelete="CASCADE"), primary_key=True
    )
    relation: Mapped[SourceRelation] = mapped_column(
        enum_column(SourceRelation, "source_relation"),
        nullable=False,
        default=SourceRelation.SHOWS_PRODUCT,
        server_default=SourceRelation.SHOWS_PRODUCT.value,
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = created_at_column()

    product: Mapped[ProductCandidate] = relationship(back_populates="sources")
    source_item: Mapped[SourceItem] = relationship(back_populates="products")

    __table_args__ = (Index("ix_product_source_source_item_id", "source_item_id"),)


class ImportBatch(Base):
    """Um arquivo importado (§5: referência ao lote para auditoria)."""

    __tablename__ = "import_batch"

    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    source_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[ImportStatus] = mapped_column(
        enum_column(ImportStatus, "import_status"),
        nullable=False,
        default=ImportStatus.RUNNING,
        server_default=ImportStatus.RUNNING.value,
    )
    started_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    rows_total: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0", default=0)
    rows_accepted: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", default=0
    )
    rows_rejected: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", default=0
    )

    errors: Mapped[list[ImportRowError]] = relationship(
        back_populates="batch", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "rows_total >= 0 AND rows_accepted >= 0 AND rows_rejected >= 0"
            " AND rows_accepted + rows_rejected <= rows_total",
            name="row_counts_consistent",
        ),
        Index("ix_import_batch_file_sha256", "file_sha256"),
    )


class ImportRowError(Base):
    """Erro de validação de uma linha do arquivo (a linha não gera observação)."""

    __tablename__ = "import_row_error"

    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    batch_id: Mapped[int] = mapped_column(
        BigId, ForeignKey("import_batch.id", ondelete="CASCADE"), nullable=False
    )
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    error: Mapped[str] = mapped_column(Text, nullable=False)
    raw_row: Mapped[dict[str, Any] | None] = mapped_column(Json)

    batch: Mapped[ImportBatch] = relationship(back_populates="errors")

    __table_args__ = (
        CheckConstraint("row_number >= 1", name="row_number_positive"),
        Index("ix_import_row_error_batch_id", "batch_id"),
    )


class Observation(Base):
    """Snapshot imutável dos campos disponíveis de um item de fonte num instante."""

    __tablename__ = "observation"

    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    source_item_id: Mapped[int] = mapped_column(
        BigId, ForeignKey("source_item.id", ondelete="RESTRICT"), nullable=False
    )
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    # Código ISO 3166-1 alfa-2 (ex.: "US"); NULL = desconhecido.
    market_region: Mapped[str | None] = mapped_column(String(2))
    region_basis: Mapped[RegionBasis] = mapped_column(
        enum_column(RegionBasis, "region_basis"),
        nullable=False,
        default=RegionBasis.UNKNOWN,
        server_default=RegionBasis.UNKNOWN.value,
    )
    views: Mapped[int | None] = mapped_column(Count)
    likes: Mapped[int | None] = mapped_column(Count)
    comments_count: Mapped[int | None] = mapped_column(Count)
    shares: Mapped[int | None] = mapped_column(Count)
    observed_price_usd: Mapped[Decimal | None] = mapped_column(Money)
    capture_method: Mapped[CaptureMethod] = mapped_column(
        enum_column(CaptureMethod, "capture_method"), nullable=False
    )
    note: Mapped[str | None] = mapped_column(Text)
    import_batch_id: Mapped[int | None] = mapped_column(
        BigId, ForeignKey("import_batch.id", ondelete="RESTRICT")
    )
    import_row_number: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = created_at_column()

    source_item: Mapped[SourceItem] = relationship(back_populates="observations")

    __table_args__ = (
        UniqueConstraint("source_item_id", "observed_at"),
        CheckConstraint(
            "(views IS NULL OR views >= 0) AND (likes IS NULL OR likes >= 0)"
            " AND (comments_count IS NULL OR comments_count >= 0)"
            " AND (shares IS NULL OR shares >= 0)",
            name="counts_non_negative",
        ),
        CheckConstraint(
            "observed_price_usd IS NULL OR observed_price_usd >= 0", name="price_non_negative"
        ),
        CheckConstraint(
            "market_region IS NULL OR (length(market_region) = 2"
            " AND market_region = upper(market_region)"
            " AND substr(market_region, 1, 1) BETWEEN 'A' AND 'Z'"
            " AND substr(market_region, 2, 1) BETWEEN 'A' AND 'Z')",
            name="market_region_iso2",
        ),
        CheckConstraint(
            "(capture_method = 'csv') = (import_batch_id IS NOT NULL)",
            name="csv_requires_batch",
        ),
        CheckConstraint(
            "(import_batch_id IS NULL AND import_row_number IS NULL) OR"
            " (import_batch_id IS NOT NULL AND import_row_number IS NOT NULL"
            " AND import_row_number >= 2)",
            name="row_number_requires_batch",
        ),
        CheckConstraint(
            "region_basis <> 'source_filter_us' OR"
            " (market_region IS NOT NULL AND market_region = 'US')",
            name="us_filter_requires_us_region",
        ),
        Index("ix_observation_observed_at", "observed_at"),
        Index("ix_observation_import_batch_id", "import_batch_id"),
    )


class SupplierOffer(Base):
    """Cotação datada de fornecedor — não é garantia de preço nem prazo."""

    __tablename__ = "supplier_offer"

    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    product_id: Mapped[int] = mapped_column(
        BigId, ForeignKey("product_candidate.id", ondelete="RESTRICT"), nullable=False
    )
    supplier_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    unit_cost_usd: Mapped[Decimal | None] = mapped_column(Money)
    shipping_usd: Mapped[Decimal | None] = mapped_column(Money)
    # Preenchidos quando a cotação original não era em USD (os campos *_usd são convertidos).
    original_currency: Mapped[str | None] = mapped_column(String(3))
    original_unit_cost: Mapped[Decimal | None] = mapped_column(Money)
    original_shipping: Mapped[Decimal | None] = mapped_column(Money)
    fx_rate_to_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    fx_rate_date: Mapped[date | None] = mapped_column(Date)
    delivery_days: Mapped[int | None] = mapped_column(Integer)
    ships_from: Mapped[str | None] = mapped_column(String(100))
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    verification_status: Mapped[VerificationStatus] = mapped_column(
        enum_column(VerificationStatus, "verification_status"),
        nullable=False,
        default=VerificationStatus.UNVERIFIED,
        server_default=VerificationStatus.UNVERIFIED.value,
    )
    created_at: Mapped[datetime] = created_at_column()

    product: Mapped[ProductCandidate] = relationship(back_populates="supplier_offers")

    __table_args__ = (
        CheckConstraint(
            "(unit_cost_usd IS NULL OR unit_cost_usd >= 0)"
            " AND (shipping_usd IS NULL OR shipping_usd >= 0)"
            " AND (original_unit_cost IS NULL OR original_unit_cost >= 0)"
            " AND (original_shipping IS NULL OR original_shipping >= 0)",
            name="amounts_non_negative",
        ),
        CheckConstraint("delivery_days IS NULL OR delivery_days >= 0", name="delivery_days_ok"),
        CheckConstraint(
            "original_currency IS NULL OR (length(original_currency) = 3"
            " AND fx_rate_to_usd IS NOT NULL AND fx_rate_to_usd > 0"
            " AND fx_rate_date IS NOT NULL)",
            name="conversion_documented",
        ),
        CheckConstraint(
            "(original_unit_cost IS NULL AND original_shipping IS NULL)"
            " OR original_currency IS NOT NULL",
            name="original_amounts_need_currency",
        ),
        Index("ix_supplier_offer_product_id", "product_id"),
    )


class Review(Base):
    """Decisão humana registrada sobre um candidato."""

    __tablename__ = "review"

    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    product_id: Mapped[int] = mapped_column(
        BigId, ForeignKey("product_candidate.id", ondelete="RESTRICT"), nullable=False
    )
    decision: Mapped[ReviewDecision] = mapped_column(
        enum_column(ReviewDecision, "review_decision"), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    reviewed_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now()
    )

    product: Mapped[ProductCandidate] = relationship(back_populates="reviews")

    __table_args__ = (
        CheckConstraint("length(trim(reason)) > 0", name="reason_not_blank"),
        Index("ix_review_product_id_reviewed_at", "product_id", "reviewed_at"),
    )


class ScoreRun(Base):
    """Resultado de uma regra de score versionada, com explicação e cobertura."""

    __tablename__ = "score_run"

    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    product_id: Mapped[int] = mapped_column(
        BigId, ForeignKey("product_candidate.id", ondelete="RESTRICT"), nullable=False
    )
    rule_version: Mapped[str] = mapped_column(String(50), nullable=False)
    computed_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now()
    )
    components_json: Mapped[dict[str, Any]] = mapped_column(Json, nullable=False)
    # Fração (0–1) dos componentes que tinham dados suficientes.
    coverage: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    # NULL = dados insuficientes para pontuar (nunca premiar dado ausente).
    score: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))

    __table_args__ = (
        CheckConstraint("coverage >= 0 AND coverage <= 1", name="coverage_fraction"),
        Index("ix_score_run_product_id_computed_at", "product_id", "computed_at"),
    )
