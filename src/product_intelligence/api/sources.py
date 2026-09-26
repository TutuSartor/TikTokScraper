"""Rotas de fontes e de suas observações.

Observações só podem ser criadas e consultadas: não há rota de edição nem de exclusão
(o banco também bloqueia UPDATE/DELETE — migração 0003).
"""

from typing import Annotated

from fastapi import APIRouter, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from product_intelligence.api.deps import DbSession, Paging, domain_http_error, not_found
from product_intelligence.api.schemas import (
    CandidateOut,
    LinkedCandidate,
    ObservationCreate,
    ObservationCreated,
    ObservationOut,
    Page,
    SourceCreate,
    SourceCreated,
    SourceDetail,
    SourceOut,
    SourceUpdate,
)
from product_intelligence.db.enums import CaptureMethod, SourceType
from product_intelligence.db.models import Observation, ProductSource, SourceItem
from product_intelligence.domain import (
    COMPARED_OBSERVATION_FIELDS,
    ConflictError,
    DomainError,
    get_or_create_source,
    record_observation,
)

router = APIRouter(tags=["sources"])


def _get(session, source_id: int) -> SourceItem:
    item = session.get(SourceItem, source_id)
    if item is None:
        not_found("fonte", source_id)
    return item


@router.post("/sources", response_model=SourceCreated, status_code=status.HTTP_201_CREATED,
             responses={200: {"description": "fonte já existia (campos vazios completados)"}})
def create_source(body: SourceCreate, session: DbSession, response: Response):
    """Cadastra a fonte pela URL normalizada. Se já existir, devolve a existente (200)."""
    try:
        item, created = get_or_create_source(
            session, body.source_type, body.url,
            body.model_dump(include={"external_id", "title", "creator_handle", "published_at"}),
        )
    except DomainError as exc:
        domain_http_error(session, exc)
    session.commit()
    if not created:
        response.status_code = status.HTTP_200_OK
    return SourceCreated(**SourceOut.model_validate(item).model_dump(), created=created)


@router.get("/sources", response_model=Page[SourceOut])
def list_sources(
    session: DbSession,
    paging: Paging,
    source_type: SourceType | None = None,
    candidate_id: Annotated[int | None, Query(ge=1)] = None,
):
    query = select(SourceItem)
    if source_type is not None:
        query = query.where(SourceItem.source_type == source_type)
    if candidate_id is not None:
        query = query.join(ProductSource).where(ProductSource.product_id == candidate_id)
    total = session.scalar(select(func.count()).select_from(query.subquery()))
    items = session.scalars(
        query.order_by(SourceItem.id).limit(paging.limit).offset(paging.offset)
    ).all()
    return Page[SourceOut](items=items, total=total, limit=paging.limit, offset=paging.offset)


@router.get("/sources/{source_id}", response_model=SourceDetail)
def get_source(source_id: int, session: DbSession):
    item = _get(session, source_id)
    links = session.scalars(
        select(ProductSource)
        .where(ProductSource.source_item_id == source_id)
        .order_by(ProductSource.created_at, ProductSource.product_id)
    ).all()
    count = session.scalar(
        select(func.count()).select_from(Observation).where(Observation.source_item_id == source_id)
    )
    last_obs = session.scalars(
        select(Observation.observed_at)
        .where(Observation.source_item_id == source_id)
        .order_by(Observation.observed_at.desc()).limit(1)
    ).first()
    return SourceDetail(
        **SourceOut.model_validate(item).model_dump(),
        candidates=[
            LinkedCandidate(relation=link.relation, reviewed_at=link.reviewed_at,
                            linked_at=link.created_at,
                            candidate=CandidateOut.model_validate(link.product))
            for link in links
        ],
        observation_count=count,
        last_observed_at=last_obs,
    )


@router.patch("/sources/{source_id}", response_model=SourceOut)
def update_source(source_id: int, body: SourceUpdate, session: DbSession):
    """Corrige metadados. Tipo e URL identificam a fonte e não podem ser alterados."""
    item = _get(session, source_id)
    for name, value in body.model_dump(exclude_unset=True).items():
        setattr(item, name, value)
    session.commit()
    return item


@router.delete("/sources/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_source(source_id: int, session: DbSession):
    """Remove fonte sem observações (e suas associações). Com observações: 409."""
    item = _get(session, source_id)
    session.delete(item)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        domain_http_error(session, ConflictError([
            f"fonte {source_id} tem observações e não pode ser apagada "
            "(observações são imutáveis)"
        ]))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------- observações ----------


@router.post("/sources/{source_id}/observations", response_model=ObservationCreated,
             status_code=status.HTTP_201_CREATED,
             responses={200: {"description": "observação idêntica já existia"},
                        409: {"description": "mesmo instante com valores diferentes"}})
def create_observation(source_id: int, body: ObservationCreate, session: DbSession,
                       response: Response):
    """Registra um snapshot manual. Mesmo instante + mesmos valores = devolve o existente."""
    _get(session, source_id)
    try:
        observation, created = record_observation(
            session, source_id, body.observed_at,
            body.model_dump(include=set(COMPARED_OBSERVATION_FIELDS)),
            capture_method=CaptureMethod.MANUAL,
        )
    except DomainError as exc:
        domain_http_error(session, exc)
    session.commit()
    if not created:
        response.status_code = status.HTTP_200_OK
    return ObservationCreated(**ObservationOut.model_validate(observation).model_dump(),
                              created=created)


@router.get("/sources/{source_id}/observations", response_model=Page[ObservationOut])
def list_observations(source_id: int, session: DbSession, paging: Paging):
    """Snapshots da fonte em ordem cronológica."""
    _get(session, source_id)
    query = select(Observation).where(Observation.source_item_id == source_id)
    total = session.scalar(select(func.count()).select_from(query.subquery()))
    items = session.scalars(
        query.order_by(Observation.observed_at, Observation.id)
        .limit(paging.limit).offset(paging.offset)
    ).all()
    return Page[ObservationOut](items=items, total=total, limit=paging.limit,
                                offset=paging.offset)


@router.get("/observations/{observation_id}", response_model=ObservationOut)
def get_observation(observation_id: int, session: DbSession):
    observation = session.get(Observation, observation_id)
    if observation is None:
        not_found("observação", observation_id)
    return observation
