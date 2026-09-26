"""Esquema do CSV de importação e validação de cada linha (ARCHITECTURE.md §5).

Regras:
- Célula vazia = desconhecido (NULL). Nunca vira zero.
- Datas em ISO 8601 COM fuso (`Z` ou `-03:00`); são convertidas para UTC.
- Contagens: inteiros ≥ 0 só com dígitos ("1200"); "1.2K", "1,200" ou "1200.0" são rejeitados
  para não adivinhar o significado.
- Preço em USD: número ≥ 0 com até 2 casas, ponto como separador decimal, sem símbolo.
- Enums precisam ser exatamente um dos valores documentados (sem diferenciar maiúsculas).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import TypeVar

from product_intelligence.db.enums import RegionBasis, SourceRelation, SourceType
from product_intelligence.ingest.urls import InvalidURL, normalize_url

REQUIRED_COLUMNS = ("product_name", "source_type", "source_url", "observed_at")
OPTIONAL_COLUMNS = (
    "product_id",
    "product_category",
    "relation",
    "market_region",
    "region_basis",
    "views",
    "likes",
    "comments_count",
    "shares",
    "observed_price_usd",
    "external_id",
    "title",
    "creator_handle",
    "published_at",
    "note",
)
ALL_COLUMNS = REQUIRED_COLUMNS + OPTIONAL_COLUMNS

# Tolerância para relógios levemente adiantados; além disso a data é considerada futura.
FUTURE_TOLERANCE = timedelta(minutes=10)

_MAX_LENGTHS = {
    "product_name": 200,
    "product_category": 100,
    "external_id": 255,
    "title": 500,
    "creator_handle": 255,
}
_DIGITS = re.compile(r"[0-9]+")
_REGION = re.compile(r"[A-Z]{2}")
_PRICE = re.compile(r"[0-9]+(\.[0-9]{1,2})?")
_BIGINT_MAX = 2**63 - 1

E = TypeVar("E", bound=StrEnum)


class RowError(Exception):
    """Uma ou mais mensagens de validação para a mesma linha."""

    def __init__(self, messages: list[str]):
        super().__init__("; ".join(messages))
        self.messages = messages


@dataclass(frozen=True)
class ParsedRow:
    product_name: str
    product_id: int | None
    product_category: str | None
    relation: SourceRelation
    source_type: SourceType
    source_url: str
    observed_at: datetime
    market_region: str | None
    region_basis: RegionBasis
    views: int | None
    likes: int | None
    comments_count: int | None
    shares: int | None
    observed_price_usd: Decimal | None
    external_id: str | None
    title: str | None
    creator_handle: str | None
    published_at: datetime | None
    note: str | None


def check_header(header: list[str] | None) -> list[str]:
    """Erros de cabeçalho (o arquivo inteiro é recusado se houver algum)."""
    if not header:
        return ["arquivo vazio ou sem cabeçalho"]
    names = [h.strip() for h in header]
    errors = []
    duplicated = sorted({n for n in names if names.count(n) > 1})
    if duplicated:
        errors.append(f"colunas repetidas: {', '.join(duplicated)}")
    missing = [c for c in REQUIRED_COLUMNS if c not in names]
    if missing:
        errors.append(f"colunas obrigatórias ausentes: {', '.join(missing)}")
    unknown = [n for n in names if n not in ALL_COLUMNS]
    if unknown:
        errors.append(
            f"colunas desconhecidas: {', '.join(unknown)} "
            f"(aceitas: {', '.join(ALL_COLUMNS)})"
        )
    return errors


def _text(raw: dict[str, str], name: str) -> str | None:
    value = (raw.get(name) or "").strip()
    return value or None


def _parse_enum(enum_cls: type[E], value: str, column: str) -> E:
    try:
        return enum_cls(value.lower())
    except ValueError:
        allowed = ", ".join(m.value for m in enum_cls)
        raise ValueError(f"{column}: '{value}' inválido (use: {allowed})") from None


def _parse_datetime(value: str, column: str, now: datetime) -> datetime:
    text = value.strip()
    if text.endswith(("z", "Z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(
            f"{column}: '{value}' não é data ISO 8601 (ex.: 2026-09-26T12:00:00Z)"
        ) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{column}: '{value}' sem fuso horário (use Z ou -03:00)")
    try:
        parsed = parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        raise ValueError(f"{column}: data fora do intervalo suportado em UTC") from None
    if parsed > now + FUTURE_TOLERANCE:
        raise ValueError(f"{column}: '{value}' está no futuro")
    return parsed


def _parse_count(value: str, column: str) -> int:
    if not _DIGITS.fullmatch(value):
        raise ValueError(f"{column}: '{value}' deve ser inteiro ≥ 0 só com dígitos")
    number = int(value)
    if number > _BIGINT_MAX:
        raise ValueError(f"{column}: '{value}' grande demais")
    return number


def parse_row(raw: dict[str, str], now: datetime | None = None) -> ParsedRow:
    """Valida uma linha do DictReader. Levanta RowError com TODAS as falhas da linha."""
    now = now or datetime.now(UTC)
    nul_columns = [name for name, value in raw.items() if "\x00" in value]
    if nul_columns:
        raise RowError([f"{name}: caractere NUL não permitido" for name in nul_columns])
    errors: list[str] = []
    values: dict[str, object] = {}

    def attempt(column: str, fn):
        try:
            values[column] = fn()
        except ValueError as exc:
            errors.append(str(exc))

    for column in REQUIRED_COLUMNS:
        if _text(raw, column) is None:
            errors.append(f"{column}: obrigatório")

    for column, limit in _MAX_LENGTHS.items():
        text = _text(raw, column)
        if text is not None and len(text) > limit:
            errors.append(f"{column}: mais de {limit} caracteres")

    product_name = _text(raw, "product_name")
    if product_name is not None:
        values["product_name"] = " ".join(product_name.split())

    if (v := _text(raw, "product_id")) is not None:
        attempt("product_id", lambda: _parse_count(v, "product_id") or _raise_zero_id())

    if (v := _text(raw, "source_type")) is not None:
        attempt("source_type", lambda: _parse_enum(SourceType, v, "source_type"))

    if (v := _text(raw, "source_url")) is not None:
        def url():
            try:
                return normalize_url(v)
            except InvalidURL as exc:
                raise ValueError(f"source_url: {exc}") from None
        attempt("source_url", url)

    if (v := _text(raw, "observed_at")) is not None:
        attempt("observed_at", lambda: _parse_datetime(v, "observed_at", now))
    if (v := _text(raw, "published_at")) is not None:
        attempt("published_at", lambda: _parse_datetime(v, "published_at", now))

    values["relation"] = SourceRelation.SHOWS_PRODUCT
    if (v := _text(raw, "relation")) is not None:
        attempt("relation", lambda: _parse_enum(SourceRelation, v, "relation"))

    values["region_basis"] = RegionBasis.UNKNOWN
    if (v := _text(raw, "region_basis")) is not None:
        attempt("region_basis", lambda: _parse_enum(RegionBasis, v, "region_basis"))

    if (v := _text(raw, "market_region")) is not None:
        attempt("market_region", lambda: parse_region(v))

    if "market_region" not in {e.split(":")[0] for e in errors}:
        try:
            check_region_basis(values.get("region_basis"), values.get("market_region"))
        except ValueError as exc:
            errors.append(str(exc))

    for column in ("views", "likes", "comments_count", "shares"):
        if (v := _text(raw, column)) is not None:
            attempt(column, lambda v=v, column=column: _parse_count(v, column))

    if (v := _text(raw, "observed_price_usd")) is not None:
        attempt("observed_price_usd", lambda: parse_price(v))

    if errors:
        raise RowError(errors)

    return ParsedRow(
        product_name=values["product_name"],
        product_id=values.get("product_id"),
        product_category=_text(raw, "product_category"),
        relation=values["relation"],
        source_type=values["source_type"],
        source_url=values["source_url"],
        observed_at=values["observed_at"],
        market_region=values.get("market_region"),
        region_basis=values["region_basis"],
        views=values.get("views"),
        likes=values.get("likes"),
        comments_count=values.get("comments_count"),
        shares=values.get("shares"),
        observed_price_usd=values.get("observed_price_usd"),
        external_id=_text(raw, "external_id"),
        title=_text(raw, "title"),
        creator_handle=_text(raw, "creator_handle"),
        published_at=values.get("published_at"),
        note=_text(raw, "note"),
    )


def _raise_zero_id() -> int:
    raise ValueError("product_id: deve ser ≥ 1")


# ---------- validadores por campo (usados pelo CSV e pela API) ----------

MAX_LENGTHS = _MAX_LENGTHS


def parse_datetime(value: str, column: str, now: datetime | None = None) -> datetime:
    """ISO 8601 com fuso, convertido para UTC; recusa datas no futuro."""
    return _parse_datetime(value, column, now or datetime.now(UTC))


def parse_count(value: str, column: str) -> int:
    """Inteiro ≥ 0 escrito só com dígitos ("1.2K", "1,200", "-5" são recusados)."""
    return _parse_count(value, column)


def parse_enum(enum_cls: type[E], value: str, column: str) -> E:
    return _parse_enum(enum_cls, value, column)


def parse_region(value: str, column: str = "market_region") -> str:
    """Duas letras; minúsculas são aceitas e convertidas ("us" → "US")."""
    region = value.strip().upper()
    if not _REGION.fullmatch(region):
        raise ValueError(f"{column}: '{value}' deve ter 2 letras (ex.: US)")
    return region


def parse_price(value: str, column: str = "observed_price_usd") -> Decimal:
    """Número ≥ 0 com até 2 casas, ponto decimal, sem símbolo de moeda."""
    if not _PRICE.fullmatch(value):
        raise ValueError(
            f"{column}: '{value}' deve ser número ≥ 0 com até 2 casas "
            "e ponto decimal, sem símbolo (ex.: 19.99)"
        )
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise ValueError(f"{column}: '{value}' inválido") from None
    if amount >= Decimal("10000000000"):
        raise ValueError(f"{column}: '{value}' grande demais")
    return amount


def check_region_basis(region_basis: RegionBasis | None, market_region: str | None) -> None:
    """Filtro "United States" na fonte só vale com market_region=US."""
    if region_basis == RegionBasis.SOURCE_FILTER_US and market_region != "US":
        raise ValueError("region_basis=source_filter_us exige market_region=US")
