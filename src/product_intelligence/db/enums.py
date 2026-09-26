"""Vocabulários controlados do modelo de dados (ARCHITECTURE.md §4).

Guardados no banco como texto + CHECK (não como ENUM nativo do PostgreSQL), o que funciona
também em SQLite e simplifica acrescentar valores: basta uma migração que recrie o CHECK.
Ao adicionar um valor aqui, crie a migração correspondente — o teste de sincronia
modelo × migração falha se esquecer.
"""

from enum import StrEnum


class CandidateStatus(StrEnum):
    NEW = "new"
    WATCH = "watch"
    RESEARCH_SUPPLIER = "research_supplier"
    TEST = "test"
    REJECT = "reject"


class SourceType(StrEnum):
    TIKTOK_TOP_ADS = "tiktok_top_ads"
    TIKTOK_TOP_PRODUCTS = "tiktok_top_products"
    TIKTOK_VIDEO = "tiktok_video"
    SUPPLIER = "supplier"
    OTHER = "other"


class SourceRelation(StrEnum):
    """Como um item de fonte se relaciona com o candidato."""

    SHOWS_PRODUCT = "shows_product"  # o item mostra exatamente este produto
    SIMILAR_PRODUCT = "similar_product"  # variação/concorrente parecido
    SUPPLIER_LISTING = "supplier_listing"  # página de fornecedor
    OTHER = "other"


class RegionBasis(StrEnum):
    """Por que acreditamos que a evidência se refere a uma região (ver §2)."""

    SOURCE_FILTER_US = "source_filter_us"  # filtro "United States" visível na fonte
    DECLARED_CREATOR = "declared_creator"  # criador declara a localização
    LANGUAGE_ONLY = "language_only"  # só o idioma — evidência fraca
    UNKNOWN = "unknown"
    OTHER = "other"


class CaptureMethod(StrEnum):
    MANUAL = "manual"
    CSV = "csv"
    APPROVED_API = "approved_api"


class VerificationStatus(StrEnum):
    UNVERIFIED = "unverified"  # preço visto na página, sem contato
    SUPPLIER_QUOTED = "supplier_quoted"  # cotação recebida do fornecedor
    VERIFIED = "verified"  # confirmado (ex.: amostra/pedido)


class ReviewDecision(StrEnum):
    WATCH = "watch"
    RESEARCH_SUPPLIER = "research_supplier"
    TEST = "test"
    REJECT = "reject"


class ImportStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
