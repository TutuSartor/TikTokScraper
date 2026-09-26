"""Regras de gravação compartilhadas pela importação CSV e pela API.

Toda entrada de dados (CSV, API e futuros conectores) deve passar por aqui, para que as
mesmas regras valham em qualquer caminho:

- Candidato: nomes são comparados ignorando maiúsculas e espaços repetidos; nomes apenas
  parecidos NÃO são unidos.
- Fonte: única por (source_type, URL normalizada). Metadados só preenchem campos vazios.
  Uma fonte gravada com a normalização antiga (query reordenada, sem âncora) bloqueia o
  cadastro para evitar duplicação ou união incorreta.
- Associação candidato ↔ fonte: relação divergente de uma associação existente é conflito.
- Observação: única por (fonte, observed_at). Repetida com os mesmos valores = duplicada;
  com valores diferentes = conflito (observações são imutáveis).

As funções não fazem commit; quem chama controla a transação.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from product_intelligence.db.enums import (
    CaptureMethod,
    RegionBasis,
    SourceRelation,
    SourceType,
)
from product_intelligence.db.models import (
    Observation,
    ProductCandidate,
    ProductSource,
    SourceItem,
)
from product_intelligence.ingest.rows import RowError

# Campos de observação comparados para decidir entre "duplicada" e "conflito".
COMPARED_OBSERVATION_FIELDS = (
    "market_region",
    "region_basis",
    "views",
    "likes",
    "comments_count",
    "shares",
    "observed_price_usd",
    "note",
)
SOURCE_FILL_FIELDS = ("external_id", "title", "creator_handle", "published_at")


class DomainError(RowError):
    """Violação de regra de negócio (a API responde 422)."""


class ConflictError(DomainError):
    """O dado conflita com o que já está gravado (a API responde 409)."""


def normalize_name(name: str) -> str:
    return " ".join(name.split())


def name_key(name: str) -> str:
    return normalize_name(name).lower()


def products_with_name(
    session: Session, name: str, *, exclude_id: int | None = None
) -> list[ProductCandidate]:
    """Candidatos cujo nome é idêntico ao informado (sem diferenciar maiúsculas/espaços).

    Compara em Python com TODOS os candidatos: um filtro SQL por lower(name) esconderia
    nomes gravados com espaços repetidos.
    """
    key = name_key(name)
    return [
        p for p in session.scalars(select(ProductCandidate).order_by(ProductCandidate.id))
        if name_key(p.name) == key and p.id != exclude_id
    ]


def legacy_source_url(url: str) -> str:
    """Forma que a URL teria na normalização antiga (query ordenada/recodificada, sem #)."""
    parts = urlsplit(url)
    old_query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)), doseq=True)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, old_query, ""))


def find_source(session: Session, source_type: SourceType, url: str) -> SourceItem | None:
    return session.scalars(
        select(SourceItem).where(
            SourceItem.source_type == source_type, SourceItem.external_url == url
        )
    ).one_or_none()


def get_or_create_source(
    session: Session, source_type: SourceType, url: str, metadata: dict[str, Any]
) -> tuple[SourceItem, bool]:
    """Devolve (fonte, criada?). `url` já deve estar normalizada (ingest.urls)."""
    item = find_source(session, source_type, url)
    if item is None:
        old_url = legacy_source_url(url)
        if old_url != url:
            legacy = session.scalar(select(SourceItem.id).where(
                SourceItem.source_type == source_type, SourceItem.external_url == old_url,
            ))
            if legacy is not None:
                raise ConflictError([
                    f"source_url: possível normalização antiga na fonte {legacy}; "
                    "revise a URL original antes de importar para evitar duplicação "
                    "ou união incorreta"
                ])
        item = SourceItem(
            source_type=source_type,
            external_url=url,
            **{name: metadata.get(name) for name in SOURCE_FILL_FIELDS},
        )
        session.add(item)
        session.flush()
        return item, True
    for name in SOURCE_FILL_FIELDS:  # só completa; nunca sobrescreve o que já existe
        if getattr(item, name) is None and metadata.get(name) is not None:
            setattr(item, name, metadata[name])
    return item, False


def link_product_source(
    session: Session, product_id: int, source_item_id: int, relation: SourceRelation
) -> tuple[ProductSource, bool]:
    """Devolve (associação, criada?). Relação diferente da existente = conflito."""
    existing = session.get(ProductSource, (product_id, source_item_id))
    if existing is None:
        link = ProductSource(product_id=product_id, source_item_id=source_item_id,
                             relation=relation)
        session.add(link)
        session.flush()
        return link, True
    if existing.relation != relation:
        raise ConflictError(
            ["relation: associação existente tem relação diferente; revise manualmente"]
        )
    return existing, False


def record_observation(
    session: Session,
    source_item_id: int,
    observed_at: datetime,
    values: dict[str, Any],
    *,
    capture_method: CaptureMethod,
    import_batch_id: int | None = None,
    import_row_number: int | None = None,
) -> tuple[Observation, bool]:
    """Devolve (observação, criada?). Mesmo instante com valores diferentes = conflito.

    `values` contém os campos de COMPARED_OBSERVATION_FIELDS; ausentes valem NULL
    (exceto region_basis, que vale `unknown`).
    """
    if values.get("region_basis") is None:
        values = {**values, "region_basis": RegionBasis.UNKNOWN}
    existing = session.scalars(
        select(Observation).where(
            Observation.source_item_id == source_item_id,
            Observation.observed_at == observed_at,
        )
    ).one_or_none()
    if existing is not None:
        diffs = [
            name for name in COMPARED_OBSERVATION_FIELDS
            if getattr(existing, name) != values.get(name)
        ]
        if diffs:
            raise ConflictError([
                f"já existe observação desta fonte em {observed_at.isoformat()} "
                f"com valores diferentes ({', '.join(diffs)}); observações são imutáveis — "
                "use outro observed_at para um novo snapshot"
            ])
        return existing, False
    observation = Observation(
        source_item_id=source_item_id,
        observed_at=observed_at,
        capture_method=capture_method,
        import_batch_id=import_batch_id,
        import_row_number=import_row_number,
        **{name: values.get(name) for name in COMPARED_OBSERVATION_FIELDS},
    )
    session.add(observation)
    session.flush()
    return observation, True
