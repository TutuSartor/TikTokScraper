"""Rotas de candidatos e de suas associações com fontes."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from product_intelligence.api.deps import DbSession, Paging, domain_http_error, not_found
from product_intelligence.api.schemas import (
    CandidateCreate,
    CandidateDetail,
    CandidateOut,
    CandidateUpdate,
    LinkCreate,
    LinkedSource,
    LinkResult,
    LinkUpdate,
    Page,
    SourceOut,
)
from product_intelligence.db.enums import CandidateStatus
from product_intelligence.db.models import ProductCandidate, ProductSource, SourceItem
from product_intelligence.domain import (
    ConflictError,
    DomainError,
    get_or_create_source,
    link_product_source,
    products_with_name,
)

router = APIRouter(prefix="/candidates", tags=["candidates"])


def _get(session, candidate_id: int) -> ProductCandidate:
    candidate = session.get(ProductCandidate, candidate_id)
    if candidate is None:
        not_found("candidato", candidate_id)
    return candidate


def _check_unique_name(session, name: str, exclude_id: int | None = None) -> None:
    """Nome idêntico a outro candidato tornaria a importação CSV ambígua."""
    same = products_with_name(session, name, exclude_id=exclude_id)
    if same:
        ids = ", ".join(str(p.id) for p in same)
        raise ConflictError([f"já existe candidato com o nome '{name}' (ids {ids})"])


def _linked(link: ProductSource) -> LinkedSource:
    return LinkedSource(
        relation=link.relation,
        reviewed_at=link.reviewed_at,
        linked_at=link.created_at,
        source=SourceOut.model_validate(link.source_item),
    )


@router.post("", response_model=CandidateOut, status_code=status.HTTP_201_CREATED)
def create_candidate(body: CandidateCreate, session: DbSession):
    try:
        _check_unique_name(session, body.name)
    except DomainError as exc:
        domain_http_error(session, exc)
    candidate = ProductCandidate(**body.model_dump())
    session.add(candidate)
    session.commit()
    return candidate


@router.get("", response_model=Page[CandidateOut])
def list_candidates(
    session: DbSession,
    paging: Paging,
    status_: Annotated[CandidateStatus | None, Query(alias="status")] = None,
    q: Annotated[str | None, Query(max_length=200, description="trecho do nome")] = None,
):
    query = select(ProductCandidate)
    if status_ is not None:
        query = query.where(ProductCandidate.status == status_)
    if q and q.strip():
        query = query.where(
            func.lower(ProductCandidate.name).contains(q.strip().lower(), autoescape=True)
        )
    total = session.scalar(select(func.count()).select_from(query.subquery()))
    items = session.scalars(
        query.order_by(ProductCandidate.id).limit(paging.limit).offset(paging.offset)
    ).all()
    return Page[CandidateOut](items=items, total=total, limit=paging.limit, offset=paging.offset)


@router.get("/{candidate_id}", response_model=CandidateDetail)
def get_candidate(candidate_id: int, session: DbSession):
    candidate = _get(session, candidate_id)
    links = session.scalars(
        select(ProductSource)
        .where(ProductSource.product_id == candidate_id)
        .order_by(ProductSource.created_at, ProductSource.source_item_id)
    ).all()
    return CandidateDetail(
        **CandidateOut.model_validate(candidate).model_dump(),
        sources=[_linked(link) for link in links],
    )


@router.patch("/{candidate_id}", response_model=CandidateOut)
def update_candidate(candidate_id: int, body: CandidateUpdate, session: DbSession):
    candidate = _get(session, candidate_id)
    changes = body.model_dump(exclude_unset=True)
    try:
        if "name" in changes:
            _check_unique_name(session, changes["name"], exclude_id=candidate_id)
    except DomainError as exc:
        domain_http_error(session, exc)
    for name, value in changes.items():
        setattr(candidate, name, value)
    session.commit()
    session.refresh(candidate)
    return candidate


@router.delete("/{candidate_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_candidate(candidate_id: int, session: DbSession):
    """Remove o candidato e suas associações. Bloqueado se houver review, cotação ou score."""
    candidate = _get(session, candidate_id)
    session.delete(candidate)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        domain_http_error(session, ConflictError([
            f"candidato {candidate_id} tem histórico (review, cotação ou score) "
            "e não pode ser apagado"
        ]))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------- associações ----------


@router.post("/{candidate_id}/sources", response_model=LinkResult,
             responses={200: {"description": "associação já existia"}},
             status_code=status.HTTP_201_CREATED)
def link_source(candidate_id: int, body: LinkCreate, session: DbSession, response: Response):
    _get(session, candidate_id)
    try:
        if body.source_id is not None:
            item = session.get(SourceItem, body.source_id)
            if item is None:
                not_found("fonte", body.source_id)
            source_created = False
        else:
            item, source_created = get_or_create_source(
                session, body.source_type, body.url,
                body.model_dump(include={"external_id", "title", "creator_handle",
                                         "published_at"}),
            )
        link, link_created = link_product_source(session, candidate_id, item.id, body.relation)
    except DomainError as exc:
        domain_http_error(session, exc)
    session.commit()
    if not link_created:
        response.status_code = status.HTTP_200_OK
    return LinkResult(**_linked(link).model_dump(), source_created=source_created,
                      link_created=link_created)


@router.patch("/{candidate_id}/sources/{source_id}", response_model=LinkedSource)
def update_link(candidate_id: int, source_id: int, body: LinkUpdate, session: DbSession):
    """Revisão manual: muda a relação e/ou marca (reviewed=true) ou desmarca a revisão."""
    link = session.get(ProductSource, (candidate_id, source_id))
    if link is None:
        not_found(f"associação do candidato {candidate_id} com a fonte", source_id)
    changes = body.model_dump(exclude_unset=True)
    if "relation" in changes:
        link.relation = changes["relation"]
    if "reviewed" in changes and changes["reviewed"] is not None:
        link.reviewed_at = datetime.now(UTC) if changes["reviewed"] else None
    session.commit()
    return _linked(link)


@router.delete("/{candidate_id}/sources/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
def unlink_source(candidate_id: int, source_id: int, session: DbSession):
    """Desfaz a associação. A fonte e suas observações continuam gravadas."""
    link = session.get(ProductSource, (candidate_id, source_id))
    if link is None:
        not_found(f"associação do candidato {candidate_id} com a fonte", source_id)
    session.delete(link)
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
