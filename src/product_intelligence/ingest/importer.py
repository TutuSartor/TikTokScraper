"""Importação idempotente de CSV (ARCHITECTURE.md §5).

Fluxo por arquivo (uma única transação):
1. Cria `import_batch` com nome e SHA-256 do arquivo.
2. Para cada linha: valida (rows.parse_row); se inválida, grava `import_row_error`.
3. Linha válida:
   - candidato: `product_id` explícito, ou nome idêntico (ignorando maiúsculas e espaços
     repetidos). Nomes apenas parecidos NÃO são unidos; nome repetido no banco = erro.
     Se não existir, cria.
   - fonte: upsert por (source_type, URL normalizada); preenche só campos vazios.
   - associação candidato ↔ fonte: cria se não existir.
   - observação: única por (fonte, observed_at). Já existente com os mesmos valores =
     duplicada (ignorada); com valores diferentes = erro (observações são imutáveis).
4. Atualiza contadores e status e faz commit. Em erro inesperado: rollback de tudo e
   o lote é registrado como `failed` (sem nenhuma linha importada).

Importar o mesmo arquivo de novo não duplica nada: todas as linhas viram "duplicadas".
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from product_intelligence.db.enums import CaptureMethod, ImportStatus
from product_intelligence.db.models import (
    ImportBatch,
    ImportRowError,
    Observation,
    ProductCandidate,
    ProductSource,
    SourceItem,
)
from product_intelligence.ingest.rows import ParsedRow, RowError, check_header, parse_row

logger = logging.getLogger(__name__)

MAX_FILE_BYTES = 20 * 1024 * 1024
_COMPARED_FIELDS = (
    "market_region",
    "region_basis",
    "views",
    "likes",
    "comments_count",
    "shares",
    "observed_price_usd",
    "note",
)
_SOURCE_FILL_FIELDS = ("external_id", "title", "creator_handle", "published_at")


class FileRejected(Exception):
    """O arquivo inteiro foi recusado (cabeçalho, codificação, tamanho)."""


@dataclass
class RowIssue:
    row_number: int
    message: str


@dataclass
class ImportResult:
    batch_id: int | None
    filename: str
    sha256: str
    status: ImportStatus
    rows_total: int = 0
    rows_accepted: int = 0
    rows_duplicate: int = 0
    rows_rejected: int = 0
    products_created: int = 0
    sources_created: int = 0
    errors: list[RowIssue] = field(default_factory=list)
    dry_run: bool = False


def _decode(data: bytes) -> str:
    if len(data) > MAX_FILE_BYTES:
        raise FileRejected(f"arquivo maior que {MAX_FILE_BYTES // (1024 * 1024)} MB")
    try:
        return data.decode("utf-8-sig")  # aceita BOM do Excel
    except UnicodeDecodeError as exc:
        raise FileRejected(
            f"arquivo não está em UTF-8 (byte {exc.start}); salve como 'CSV UTF-8'"
        ) from None


def _find_product(session: Session, row: ParsedRow) -> tuple[ProductCandidate, bool]:
    """Devolve (candidato, criado?). Levanta RowError em caso ambíguo."""
    if row.product_id is not None:
        product = session.get(ProductCandidate, row.product_id)
        if product is None:
            raise RowError([f"product_id: {row.product_id} não existe"])
        if " ".join(product.name.split()).lower() != row.product_name.lower():
            raise RowError([
                f"product_id {row.product_id} é '{product.name}', "
                f"mas a linha diz '{row.product_name}'"
            ])
        return product, False

    # A mesma comparação precisa considerar TODOS os candidatos; uma pré-seleção
    # por lower(name) pode ocultar um segundo candidato com espaços repetidos.
    matches = [
        p for p in session.scalars(select(ProductCandidate))
        if " ".join(p.name.split()).lower() == row.product_name.lower()
    ]
    if len(matches) > 1:
        ids = ", ".join(str(p.id) for p in matches)
        raise RowError([
            f"product_name '{row.product_name}' corresponde a vários candidatos (ids {ids}); "
            "informe product_id"
        ])
    if matches:
        return matches[0], False
    product = ProductCandidate(name=row.product_name, category=row.product_category)
    session.add(product)
    session.flush()
    return product, True


def _upsert_source(session: Session, row: ParsedRow) -> tuple[SourceItem, bool]:
    item = session.scalars(
        select(SourceItem).where(
            SourceItem.source_type == row.source_type,
            SourceItem.external_url == row.source_url,
        )
    ).one_or_none()
    if item is None:
        # A versão anterior ordenava/recodificava a query e removia todas as rotas #.
        # Não unir fontes por essa regra insegura nem duplicar silenciosamente dados antigos.
        parts = urlsplit(row.source_url)
        old_query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)), doseq=True)
        old_url = urlunsplit((parts.scheme, parts.netloc, parts.path, old_query, ""))
        if old_url != row.source_url:
            legacy = session.scalar(select(SourceItem.id).where(
                SourceItem.source_type == row.source_type, SourceItem.external_url == old_url,
            ))
            if legacy is not None:
                raise RowError([
                    f"source_url: possível normalização antiga na fonte {legacy}; "
                    "revise a URL original antes de importar para evitar duplicação "
                    "ou união incorreta"
                ])
        item = SourceItem(
            source_type=row.source_type,
            external_url=row.source_url,
            **{name: getattr(row, name) for name in _SOURCE_FILL_FIELDS},
        )
        session.add(item)
        session.flush()
        return item, True
    for name in _SOURCE_FILL_FIELDS:  # só completa; nunca sobrescreve o que já existe
        if getattr(item, name) is None and getattr(row, name) is not None:
            setattr(item, name, getattr(row, name))
    return item, False


def _link(session: Session, product: ProductCandidate, item: SourceItem, row: ParsedRow) -> None:
    existing = session.get(ProductSource, (product.id, item.id))
    if existing is None:
        session.add(ProductSource(product_id=product.id, source_item_id=item.id,
                                  relation=row.relation))
        session.flush()
    elif existing.relation != row.relation:
        raise RowError(["relation: associação existente tem relação diferente; revise manualmente"])


def _observation(
    session: Session, item: SourceItem, row: ParsedRow, batch: ImportBatch, row_number: int
) -> bool:
    """Grava a observação. Devolve False se for duplicada idêntica; RowError se conflitar."""
    existing = session.scalars(
        select(Observation).where(
            Observation.source_item_id == item.id,
            Observation.observed_at == row.observed_at,
        )
    ).one_or_none()
    if existing is not None:
        diffs = [
            name for name in _COMPARED_FIELDS if getattr(existing, name) != getattr(row, name)
        ]
        if diffs:
            raise RowError([
                f"já existe observação desta fonte em {row.observed_at.isoformat()} "
                f"com valores diferentes ({', '.join(diffs)}); observações são imutáveis — "
                "use outro observed_at para um novo snapshot"
            ])
        return False
    session.add(Observation(
        source_item_id=item.id,
        observed_at=row.observed_at,
        market_region=row.market_region,
        region_basis=row.region_basis,
        views=row.views,
        likes=row.likes,
        comments_count=row.comments_count,
        shares=row.shares,
        observed_price_usd=row.observed_price_usd,
        capture_method=CaptureMethod.CSV,
        note=row.note,
        import_batch_id=batch.id,
        import_row_number=row_number,
    ))
    session.flush()
    return True


def import_csv(
    session: Session,
    data: bytes,
    filename: str,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
) -> ImportResult:
    """Importa o conteúdo de um CSV. Faz commit (ou rollback se dry_run)."""
    now = now or datetime.now(UTC)
    sha256 = hashlib.sha256(data).hexdigest()
    filename = (filename or "stdin.csv")[-255:]
    result = ImportResult(batch_id=None, filename=filename, sha256=sha256,
                          status=ImportStatus.RUNNING, dry_run=dry_run)
    batch = None
    try:
        text = _decode(data)
        reader = csv.reader(io.StringIO(text, newline=""), strict=True)
        header = next(reader, None)
        header_errors = check_header(header)
        if header_errors:
            raise FileRejected("; ".join(header_errors))
        header = [name.strip() for name in header]

        batch = ImportBatch(source_filename=filename, file_sha256=sha256,
                            status=ImportStatus.RUNNING)
        session.add(batch)
        session.flush()
        result.batch_id = batch.id

        # A referência é a linha física onde o registro começa, mesmo com campos multilinha.
        while True:
            row_number = reader.line_num + 1
            try:
                cells = next(reader)
            except StopIteration:
                break
            if not any(cell.strip() for cell in cells):
                continue
            raw = {name: cells[i] if i < len(cells) else None for i, name in enumerate(header)}
            if len(cells) > len(header):
                raw[None] = cells[len(header):]
            result.rows_total += 1
            if None in raw or any(v is None for v in raw.values()):
                _reject(session, batch, result, row_number, raw,
                        ["número de colunas diferente do cabeçalho"])
                continue
            try:
                row = parse_row(raw, now=now)
                with session.begin_nested():
                    product, product_created = _find_product(session, row)
                    item, source_created = _upsert_source(session, row)
                    _link(session, product, item, row)
                    inserted = _observation(session, item, row, batch, row_number)
            except RowError as exc:
                _reject(session, batch, result, row_number, raw, exc.messages)
                continue
            except IntegrityError as exc:
                logger.warning("Linha %s violou restrição: %s", row_number, exc.orig)
                _reject(session, batch, result, row_number, raw,
                        [f"violação de integridade no banco: {exc.orig}"])
                continue
            result.products_created += product_created
            result.sources_created += source_created
            if inserted:
                result.rows_accepted += 1
            else:
                result.rows_duplicate += 1

        batch.rows_total = result.rows_total
        batch.rows_accepted = result.rows_accepted
        batch.rows_rejected = result.rows_rejected
        batch.status = ImportStatus.COMPLETED
        batch.finished_at = datetime.now(UTC)
        result.status = ImportStatus.COMPLETED
        if dry_run:
            session.rollback()
            result.batch_id = None
        else:
            session.commit()
        return result
    except (FileRejected, csv.Error) as exc:
        session.rollback()
        message = str(exc) if isinstance(exc, FileRejected) else f"CSV malformado: {exc}"
        return _record_failure(session, result, message, dry_run)
    except Exception:
        session.rollback()
        _record_failure(session, result, "erro inesperado (ver log)", dry_run)
        raise


def _reject(session, batch, result: ImportResult, row_number: int, raw, messages) -> None:
    clean = {k: v for k, v in raw.items() if k is not None}
    if None in raw:
        clean["_extra"] = raw[None]
    message = "; ".join(messages)
    result.rows_rejected += 1
    result.errors.append(RowIssue(row_number, message))
    session.add(ImportRowError(batch_id=batch.id, row_number=row_number, error=message,
                               raw_row=_safe_raw_row(clean)))
    session.flush()


def _safe_raw_row(value):
    """JSONB não aceita NUL: guarda essas células em base64 reversível, sem perder o original."""
    if isinstance(value, str) and "\x00" in value:
        encoded = base64.b64encode(value.encode()).decode("ascii")
        return {"encoding": "base64-utf8", "value": encoded}
    if isinstance(value, dict):
        return {key: _safe_raw_row(cell) for key, cell in value.items()}
    if isinstance(value, list):
        return [_safe_raw_row(cell) for cell in value]
    return value


def _record_failure(session: Session, result: ImportResult, message: str,
                    dry_run: bool) -> ImportResult:
    """Registra o lote como failed (sem linhas importadas) numa transação nova."""
    result.status = ImportStatus.FAILED
    result.rows_total = 0
    result.rows_accepted = result.rows_duplicate = result.rows_rejected = 0
    result.products_created = result.sources_created = 0
    result.errors = [RowIssue(0, message)]
    result.batch_id = None
    if dry_run:
        return result
    batch = ImportBatch(source_filename=result.filename, file_sha256=result.sha256,
                        status=ImportStatus.FAILED, finished_at=datetime.now(UTC))
    batch.errors.append(ImportRowError(row_number=1, error=message))
    session.add(batch)
    session.commit()
    result.batch_id = batch.id
    return result
