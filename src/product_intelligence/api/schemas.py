"""Esquemas de entrada/saída da API.

A validação de cada campo reaproveita os validadores do CSV (ingest.rows) para que a
API e a importação aceitem e recusem exatamente os mesmos valores:
- datas: texto ISO 8601 COM fuso, convertido para UTC; futuro é recusado;
- contagens: inteiro ≥ 0 (número JSON ou texto só com dígitos);
- preço USD: até 2 casas, ≥ 0 (ex.: "19.99" ou 19.99), sem símbolo;
- região: 2 letras, convertida para maiúsculas;
- URL: normalizada por ingest.urls.normalize_url.
Campos desconhecidos são recusados (evita erros de digitação silenciosos).
Ausente ou null = desconhecido; nunca vira zero.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Generic, TypeVar

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from product_intelligence.db.enums import (
    CandidateStatus,
    CaptureMethod,
    RegionBasis,
    SourceRelation,
    SourceType,
)
from product_intelligence.domain import normalize_name
from product_intelligence.ingest.rows import (
    MAX_LENGTHS,
    check_region_basis,
    parse_count,
    parse_datetime,
    parse_price,
    parse_region,
)
from product_intelligence.ingest.urls import InvalidURL, normalize_url

T = TypeVar("T")


def _reject_nul(value: Any) -> Any:
    if isinstance(value, str) and "\x00" in value:
        raise ValueError("caractere NUL não permitido")
    return value


def _text(max_length: int | None = None):
    """Texto opcional: aparado; vazio vira null."""
    def validate(value: Any) -> Any:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("deve ser texto")
        _reject_nul(value)
        value = value.strip()
        if not value:
            return None
        if max_length is not None and len(value) > max_length:
            raise ValueError(f"mais de {max_length} caracteres")
        return value
    return BeforeValidator(validate)


def _datetime(column: str):
    def validate(value: Any) -> Any:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError(f"{column}: use texto ISO 8601 com fuso (ex.: 2026-09-26T12:00:00Z)")
        return parse_datetime(value, column)
    return BeforeValidator(validate)


def _count(column: str):
    def validate(value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int | str):
            raise ValueError(f"{column}: deve ser inteiro ≥ 0")
        return parse_count(str(value).strip(), column)
    return BeforeValidator(validate)


def _price(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float | str | Decimal):
        raise ValueError("observed_price_usd: deve ser número")
    return parse_price(str(value).strip())


def _region(value: Any) -> Any:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("market_region: deve ser texto de 2 letras")
    return parse_region(value)


OptionalText = Annotated[str | None, _text()]
Category = Annotated[str | None, _text(MAX_LENGTHS["product_category"])]
ExternalId = Annotated[str | None, _text(MAX_LENGTHS["external_id"])]
Title = Annotated[str | None, _text(MAX_LENGTHS["title"])]
CreatorHandle = Annotated[str | None, _text(MAX_LENGTHS["creator_handle"])]
PublishedAt = Annotated[datetime | None, _datetime("published_at")]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------- candidatos ----------


def _product_name(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("deve ser texto")
    _reject_nul(value)
    name = normalize_name(value)
    if not name:
        raise ValueError("obrigatório")
    if len(name) > MAX_LENGTHS["product_name"]:
        raise ValueError(f"mais de {MAX_LENGTHS['product_name']} caracteres")
    return name


ProductName = Annotated[str, BeforeValidator(_product_name)]


class CandidateCreate(StrictModel):
    name: ProductName
    category: Category = None
    description: OptionalText = None


class CandidateUpdate(StrictModel):
    """PATCH: só os campos enviados mudam. O status muda por review (Fase 4)."""

    name: ProductName | None = None
    category: Category = None
    description: OptionalText = None

    @field_validator("name")
    @classmethod
    def _name_not_null(cls, value):
        if value is None:
            raise ValueError("name não pode ser null")
        return value


class CandidateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    category: str | None
    description: str | None
    status: CandidateStatus
    created_at: datetime
    updated_at: datetime


# ---------- fontes ----------


class SourceMetadata(StrictModel):
    external_id: ExternalId = None
    title: Title = None
    creator_handle: CreatorHandle = None
    published_at: PublishedAt = None


def _url(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("deve ser texto")
    _reject_nul(value)
    try:
        return normalize_url(value)
    except InvalidURL as exc:
        raise ValueError(str(exc)) from None


SourceUrl = Annotated[str, BeforeValidator(_url)]


class SourceCreate(SourceMetadata):
    source_type: SourceType
    url: SourceUrl


class SourceUpdate(SourceMetadata):
    """PATCH de metadados. Tipo e URL identificam a fonte e não mudam."""


class SourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source_type: SourceType
    external_url: str
    external_id: str | None
    title: str | None
    creator_handle: str | None
    published_at: datetime | None
    created_at: datetime


class SourceCreated(SourceOut):
    created: bool


# ---------- associação candidato ↔ fonte ----------


class LinkCreate(StrictModel):
    """Associa uma fonte ao candidato: por `source_id` OU por `source_type` + `url`
    (a fonte é criada se não existir, pelas mesmas regras do CSV)."""

    relation: SourceRelation = SourceRelation.SHOWS_PRODUCT
    source_id: int | None = Field(default=None, ge=1)
    source_type: SourceType | None = None
    url: SourceUrl | None = None
    external_id: ExternalId = None
    title: Title = None
    creator_handle: CreatorHandle = None
    published_at: PublishedAt = None

    @model_validator(mode="after")
    def _one_way(self):
        by_id = self.source_id is not None
        by_url = self.source_type is not None or self.url is not None
        if by_id == by_url:
            raise ValueError("informe source_id OU (source_type e url)")
        if by_url and (self.source_type is None or self.url is None):
            raise ValueError("source_type e url são obrigatórios juntos")
        if by_id and any(
            getattr(self, name) is not None
            for name in ("external_id", "title", "creator_handle", "published_at")
        ):
            raise ValueError("metadados só podem ser enviados junto com source_type e url")
        return self


class LinkUpdate(StrictModel):
    """Revisão manual da associação (ex.: corrigir agrupamento errado)."""

    relation: SourceRelation | None = None
    reviewed: bool | None = None

    @field_validator("relation")
    @classmethod
    def _relation_not_null(cls, value):
        if value is None:
            raise ValueError("relation não pode ser null")
        return value


class LinkedSource(BaseModel):
    relation: SourceRelation
    reviewed_at: datetime | None
    linked_at: datetime
    source: SourceOut


class LinkResult(LinkedSource):
    source_created: bool
    link_created: bool


class LinkedCandidate(BaseModel):
    relation: SourceRelation
    reviewed_at: datetime | None
    linked_at: datetime
    candidate: CandidateOut


class CandidateDetail(CandidateOut):
    sources: list[LinkedSource]


class SourceDetail(SourceOut):
    candidates: list[LinkedCandidate]
    observation_count: int
    last_observed_at: datetime | None


# ---------- observações ----------


class ObservationCreate(StrictModel):
    observed_at: Annotated[datetime, _datetime("observed_at")]
    market_region: Annotated[str | None, BeforeValidator(_region)] = None
    region_basis: RegionBasis = RegionBasis.UNKNOWN
    views: Annotated[int | None, _count("views")] = None
    likes: Annotated[int | None, _count("likes")] = None
    comments_count: Annotated[int | None, _count("comments_count")] = None
    shares: Annotated[int | None, _count("shares")] = None
    observed_price_usd: Annotated[Decimal | None, BeforeValidator(_price)] = None
    note: OptionalText = None

    @model_validator(mode="after")
    def _region_basis(self):
        check_region_basis(self.region_basis, self.market_region)
        return self


class ObservationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source_item_id: int
    observed_at: datetime
    market_region: str | None
    region_basis: RegionBasis
    views: int | None
    likes: int | None
    comments_count: int | None
    shares: int | None
    observed_price_usd: Decimal | None
    capture_method: CaptureMethod
    note: str | None
    import_batch_id: int | None
    import_row_number: int | None
    created_at: datetime


class ObservationCreated(ObservationOut):
    created: bool


# ---------- paginação ----------


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int
