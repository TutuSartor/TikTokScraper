"""Comando de importação de CSV.

Uso (na VM, com o Compose ativo):
    docker compose exec -T api python -m product_intelligence.jobs.import_csv - \\
        --name candidatos_2026-09-26.csv < candidatos_2026-09-26.csv

    # só validar, sem gravar nada:
    docker compose exec -T api python -m product_intelligence.jobs.import_csv - \\
        --name arquivo.csv --dry-run < arquivo.csv

Códigos de saída: 0 = todas as linhas aceitas ou duplicadas; 2 = algumas linhas rejeitadas
(as demais foram gravadas); 1 = arquivo recusado ou erro; 3 = outra
importação em andamento.
Arquivos recusados durante o processamento deixam somente um lote de falha para auditoria.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from product_intelligence.db.enums import ImportStatus
from product_intelligence.db.session import get_engine
from product_intelligence.ingest.importer import MAX_FILE_BYTES, ImportResult, import_csv

logger = logging.getLogger(__name__)

# Chave arbitrária e fixa para pg_try_advisory_lock (uma importação por vez).
LOCK_KEY = 7_140_263_001
MAX_ERRORS_SHOWN = 50


class ImportBusy(RuntimeError):
    """A trava está ocupada; não confundir com falha ao consultar o banco."""


def _acquire_lock(engine: Engine):
    """Devolve a conexão que segura a trava (ou None se não há trava no dialeto)."""
    if engine.dialect.name != "postgresql":
        return None
    conn = engine.connect()
    try:
        acquired = conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": LOCK_KEY}).scalar()
        if not acquired:
            raise ImportBusy("outra importação está em andamento")
    except ImportBusy:
        conn.close()
        raise
    except BaseException:
        try:
            conn.invalidate()
        finally:
            conn.close()
        raise
    return conn


def _release_lock(conn) -> None:
    if conn is not None:
        try:
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_KEY})
        except SQLAlchemyError:
            # Não devolver ao pool uma conexão que pode ainda segurar a trava.
            logger.warning("Falha ao liberar trava; conexão descartada")
            conn.invalidate()
        finally:
            conn.close()


def format_result(result: ImportResult) -> str:
    mode = " (SIMULAÇÃO — nada foi gravado)" if result.dry_run else ""
    lines = [
        f"Arquivo: {result.filename}{mode}",
        f"SHA-256: {result.sha256}",
        f"Lote: {result.batch_id if result.batch_id is not None else '-'}"
        f" | status: {result.status.value}",
    ]
    if result.status == ImportStatus.FAILED:
        lines.append(f"ARQUIVO RECUSADO: {result.errors[0].message}")
        return "\n".join(lines)
    lines += [
        f"Linhas: {result.rows_total} | novas: {result.rows_accepted}"
        f" | duplicadas (já existiam): {result.rows_duplicate}"
        f" | rejeitadas: {result.rows_rejected}",
        f"Candidatos criados: {result.products_created}"
        f" | fontes criadas: {result.sources_created}",
    ]
    if result.errors:
        lines.append("Erros por linha (linha 1 = cabeçalho):")
        for issue in result.errors[:MAX_ERRORS_SHOWN]:
            lines.append(f"  linha {issue.row_number}: {issue.message}")
        if len(result.errors) > MAX_ERRORS_SHOWN:
            lines.append(f"  ... e mais {len(result.errors) - MAX_ERRORS_SHOWN} "
                         f"(consulte import_row_error, lote {result.batch_id})")
    return "\n".join(lines)


def run(argv: list[str] | None = None, engine: Engine | None = None,
        stdin=None, stdout=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m product_intelligence.jobs.import_csv",
        description="Importa um CSV de observações (ver README, seção Importação CSV).",
    )
    parser.add_argument("path", help="caminho do CSV, ou '-' para ler da entrada padrão")
    parser.add_argument("--name", help="nome do arquivo a registrar (obrigatório com '-')")
    parser.add_argument("--dry-run", action="store_true",
                        help="valida e mostra o resultado sem gravar nada")
    args = parser.parse_args(argv)
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout

    if args.path == "-":
        if not args.name:
            parser.error("--name é obrigatório ao ler da entrada padrão")
        try:
            data, name = stdin.read(MAX_FILE_BYTES + 1), args.name
        except OSError as exc:
            print(f"falha ao ler entrada padrão: {exc}", file=stdout)
            return 1
    else:
        path = Path(args.path)
        if not path.is_file():
            print(f"arquivo não encontrado: {path}", file=stdout)
            return 1
        try:
            with path.open("rb") as source:
                data = source.read(MAX_FILE_BYTES + 1)
            name = args.name or path.name
        except OSError as exc:
            print(f"falha ao ler arquivo: {exc}", file=stdout)
            return 1

    if len(data) > MAX_FILE_BYTES:
        # Lemos só o prefixo para impor o limite, sem registrá-lo como SHA do arquivo inteiro.
        print(f"ARQUIVO RECUSADO: arquivo maior que {MAX_FILE_BYTES} bytes", file=stdout)
        return 1

    lock = None
    try:
        engine = engine or get_engine()
        lock = _acquire_lock(engine)
        with Session(engine) as session:
            result = import_csv(session, data, name, dry_run=args.dry_run)
    except ImportBusy as exc:
        print(str(exc), file=stdout)
        return 3
    except Exception:
        logger.exception("Falha ao importar CSV")
        print("falha na importação ou conexão com o banco (ver log)", file=stdout)
        return 1
    finally:
        _release_lock(lock)

    print(format_result(result), file=stdout)
    if result.status == ImportStatus.FAILED:
        return 1
    return 2 if result.rows_rejected else 0


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(run())


if __name__ == "__main__":
    main()
