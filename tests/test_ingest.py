"""Importação CSV: normalização, validação por linha, idempotência e comando."""

import base64
import io
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from product_intelligence.db.enums import (
    CaptureMethod,
    ImportStatus,
    RegionBasis,
    SourceRelation,
    SourceType,
)
from product_intelligence.db.models import (
    ImportBatch,
    ImportRowError,
    Observation,
    ProductCandidate,
    ProductSource,
    SourceItem,
)
from product_intelligence.ingest.importer import import_csv
from product_intelligence.ingest.rows import RowError, check_header, parse_row
from product_intelligence.ingest.urls import InvalidURL, normalize_url
from product_intelligence.jobs import import_csv as cli
from tests.test_models import engine  # noqa: F401 — fixture: banco migrado (SQLite/PostgreSQL)

NOW = datetime(2026, 9, 27, 0, 0, tzinfo=UTC)
HEADER = (
    "product_name,source_type,source_url,observed_at,market_region,region_basis,"
    "views,likes,note"
)
ROW_A = ("Pet hair remover,tiktok_top_ads,https://example.com/ad/123,2026-09-26T12:00:00Z,"
         "US,source_filter_us,120000,4300,Exemplo ficticio")
ROW_B = "Lint roller,tiktok_video,https://example.com/v/9,2026-09-26T09:00:00-03:00,,,500,,"


def csv_bytes(*rows: str, header: str = HEADER) -> bytes:
    return ("\n".join((header, *rows)) + "\n").encode("utf-8")


@pytest.fixture
def session(engine):  # noqa: F811
    with Session(engine) as s:
        yield s


def count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


def run_import(session, data, name="a.csv", **kw):
    return import_csv(session, data, name, now=NOW, **kw)


# ---------- URL ----------


@pytest.mark.parametrize("raw,expected", [
    ("https://Example.COM/ad/123", "https://example.com/ad/123"),
    ("HTTPS://example.com:443/ad/123#top", "https://example.com/ad/123"),
    ("https://example.com/ad/123?utm_source=x&b=2&a=1&fbclid=z",
     "https://example.com/ad/123?b=2&a=1"),
    ("https://example.com", "https://example.com/"),
    ("  https://www.tiktok.com/@u/video/1?_r=1&_t=abc  ",
     "https://www.tiktok.com/@u/video/1?_r=1&_t=abc"),
    ("http://example.com:8080/x", "http://example.com:8080/x"),
])
def test_normalize_url(raw, expected):
    assert normalize_url(raw) == expected


@pytest.mark.parametrize("raw", [
    "", "example.com/x", "ftp://example.com/x", "https://localhost/x", "https://a b.com",
    "https://user:pw@example.com/", "https://example.com:99999/",
])
def test_normalize_url_rejects(raw):
    with pytest.raises(InvalidURL):
        normalize_url(raw)


# ---------- validação de linha ----------


def raw_row(**overrides):
    base = {"product_name": "P", "source_type": "tiktok_video",
            "source_url": "https://example.com/v/1", "observed_at": "2026-09-26T12:00:00Z"}
    base.update(overrides)
    return base


def test_empty_cells_are_null_not_zero():
    row = parse_row(raw_row(views="", likes="  ", observed_price_usd=""), now=NOW)
    assert row.views is None and row.likes is None and row.observed_price_usd is None
    assert row.region_basis == RegionBasis.UNKNOWN
    assert row.relation == SourceRelation.SHOWS_PRODUCT


def test_row_normalizes_values():
    row = parse_row(raw_row(product_name="  Pet   hair  remover ", market_region="us",
                            source_type="TikTok_Video", observed_at="2026-09-26T09:00:00-03:00",
                            observed_price_usd="19.9", views="0"), now=NOW)
    assert row.product_name == "Pet hair remover"
    assert row.market_region == "US"
    assert row.source_type == SourceType.TIKTOK_VIDEO
    assert row.observed_at == datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    assert row.observed_price_usd == Decimal("19.9")
    assert row.views == 0  # zero explícito continua zero


@pytest.mark.parametrize("overrides,fragment", [
    ({"product_name": ""}, "product_name: obrigatório"),
    ({"observed_at": "2026-09-26T12:00:00"}, "sem fuso"),
    ({"observed_at": "26/09/2026"}, "ISO 8601"),
    ({"observed_at": "2026-12-01T00:00:00Z"}, "no futuro"),
    ({"views": "1.2K"}, "views"),
    ({"views": "1,200"}, "views"),
    ({"views": "-5"}, "views"),
    ({"views": "1200.0"}, "views"),
    ({"observed_price_usd": "$19.99"}, "observed_price_usd"),
    ({"observed_price_usd": "19,99"}, "observed_price_usd"),
    ({"observed_price_usd": "19.999"}, "observed_price_usd"),
    ({"market_region": "USA"}, "market_region"),
    ({"region_basis": "source_filter_us"}, "exige market_region=US"),
    ({"region_basis": "source_filter_us", "market_region": "BR"}, "exige market_region=US"),
    ({"source_type": "instagram"}, "source_type"),
    ({"relation": "same"}, "relation"),
    ({"source_url": "example.com/x"}, "source_url"),
    ({"product_id": "0"}, "product_id"),
    ({"title": "x" * 501}, "title"),
])
def test_row_errors(overrides, fragment):
    with pytest.raises(RowError) as exc:
        parse_row(raw_row(**overrides), now=NOW)
    assert fragment in str(exc.value)


def test_row_reports_all_errors_at_once():
    with pytest.raises(RowError) as exc:
        parse_row(raw_row(views="-1", likes="abc", source_type="x"), now=NOW)
    assert len(exc.value.messages) == 3


def test_header_checks():
    assert check_header(HEADER.split(",")) == []
    assert "ausentes: observed_at" in check_header(["product_name", "source_type", "source_url"])[0]
    assert "desconhecidas: view" in check_header(HEADER.split(",") + ["view"])[0]
    assert "repetidas: views" in check_header(HEADER.split(",") + ["views"])[0]


# ---------- importação ----------


def test_import_creates_everything_with_provenance(session):
    result = run_import(session, csv_bytes(ROW_A, ROW_B))
    assert result.status == ImportStatus.COMPLETED
    assert (result.rows_total, result.rows_accepted, result.rows_rejected) == (2, 2, 0)
    assert (result.products_created, result.sources_created) == (2, 2)

    batch = session.get(ImportBatch, result.batch_id)
    assert batch.status == ImportStatus.COMPLETED
    assert (batch.rows_total, batch.rows_accepted, batch.rows_rejected) == (2, 2, 0)
    assert batch.finished_at is not None and len(batch.file_sha256) == 64

    obs = session.scalars(select(Observation).order_by(Observation.import_row_number)).all()
    assert [o.import_row_number for o in obs] == [2, 3]
    assert all(o.capture_method == CaptureMethod.CSV and o.import_batch_id == batch.id
               for o in obs)
    a, b = obs
    assert (a.views, a.likes, a.market_region) == (120000, 4300, "US")
    assert a.region_basis == RegionBasis.SOURCE_FILTER_US
    # Linha B: campos vazios continuam desconhecidos.
    assert b.likes is None and b.market_region is None and b.comments_count is None
    assert b.region_basis == RegionBasis.UNKNOWN
    assert b.observed_at == datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    assert count(session, ProductSource) == 2


def test_reimporting_same_file_creates_no_duplicates(session):
    data = csv_bytes(ROW_A, ROW_B)
    first = run_import(session, data)
    second = run_import(session, data)
    assert second.status == ImportStatus.COMPLETED
    assert (second.rows_accepted, second.rows_duplicate, second.rows_rejected) == (0, 2, 0)
    assert (second.products_created, second.sources_created) == (0, 0)
    assert first.sha256 == second.sha256 and first.batch_id != second.batch_id
    assert count(session, Observation) == 2
    assert count(session, SourceItem) == 2
    assert count(session, ProductCandidate) == 2
    assert count(session, ProductSource) == 2
    assert count(session, ImportBatch) == 2  # os dois lotes ficam registrados


def test_url_variants_and_same_instant_in_other_offset_are_the_same(session):
    run_import(session, csv_bytes(ROW_A))
    variant = ("pet hair  REMOVER,tiktok_top_ads,https://EXAMPLE.com/ad/123?utm_campaign=x#frag,"
               "2026-09-26T09:00:00-03:00,us,source_filter_us,120000,4300,Exemplo ficticio")
    result = run_import(session, csv_bytes(variant), name="b.csv")
    assert (result.rows_accepted, result.rows_duplicate) == (0, 1)
    assert count(session, SourceItem) == 1 and count(session, ProductCandidate) == 1


def test_new_snapshot_of_existing_source_is_added(session):
    run_import(session, csv_bytes(ROW_A))
    later = ROW_A.replace("2026-09-26T12:00:00Z", "2026-09-26T18:00:00Z").replace(
        "120000,4300", "150000,5000")
    result = run_import(session, csv_bytes(later), name="b.csv")
    assert result.rows_accepted == 1
    assert count(session, Observation) == 2 and count(session, SourceItem) == 1


def test_conflicting_values_for_same_snapshot_are_rejected(session):
    run_import(session, csv_bytes(ROW_A))
    conflict = ROW_A.replace("120000", "999999")
    result = run_import(session, csv_bytes(conflict), name="b.csv")
    assert result.rows_rejected == 1
    assert "valores diferentes (views)" in result.errors[0].message
    assert session.scalars(select(Observation)).one().views == 120000


def test_bad_rows_are_recorded_and_good_rows_still_imported(session):
    bad = "Broken,tiktok_video,https://example.com/v/2,2026-09-26T12:00:00,,,-5,,"
    ragged = "Short,tiktok_video,https://example.com/v/3"
    result = run_import(session, csv_bytes(ROW_A, bad, ragged, ROW_B))
    assert (result.rows_total, result.rows_accepted, result.rows_rejected) == (4, 2, 2)
    errors = session.scalars(select(ImportRowError).order_by(ImportRowError.row_number)).all()
    assert [e.row_number for e in errors] == [3, 4]
    assert "sem fuso" in errors[0].error and "views" in errors[0].error
    assert errors[0].raw_row["product_name"] == "Broken"
    assert "número de colunas" in errors[1].error
    assert count(session, Observation) == 2
    assert count(session, ProductCandidate) == 2  # linhas ruins não criam candidatos


def test_same_snapshot_twice_in_one_file_counts_as_duplicate(session):
    result = run_import(session, csv_bytes(ROW_A, ROW_A))
    assert (result.rows_accepted, result.rows_duplicate) == (1, 1)


def test_blank_lines_and_excel_bom_are_accepted(session):
    data = b"\xef\xbb\xbf" + csv_bytes(ROW_A, ",,,,,,,,", ROW_B)
    result = run_import(session, data)
    assert (result.rows_total, result.rows_accepted) == (2, 2)


def test_optional_columns(session):
    header = ("product_name,product_category,relation,source_type,source_url,observed_at,"
              "comments_count,shares,observed_price_usd,external_id,title,creator_handle,"
              "published_at")
    row = ("Mini blender,Kitchen,similar_product,tiktok_video,https://example.com/v/5,"
           "2026-09-26T12:00:00Z,12,3,24.99,5,Blender demo,@maker,2026-09-20T00:00:00Z")
    result = run_import(session, csv_bytes(row, header=header))
    assert result.rows_accepted == 1
    product = session.scalars(select(ProductCandidate)).one()
    item = session.scalars(select(SourceItem)).one()
    link = session.scalars(select(ProductSource)).one()
    obs = session.scalars(select(Observation)).one()
    assert product.category == "Kitchen"
    assert link.relation == SourceRelation.SIMILAR_PRODUCT
    assert (item.title, item.creator_handle, item.external_id) == ("Blender demo", "@maker", "5")
    assert item.published_at == datetime(2026, 9, 20, tzinfo=UTC)
    assert (obs.comments_count, obs.shares, obs.observed_price_usd) == (12, 3, Decimal("24.99"))
    assert obs.views is None


def test_source_metadata_is_completed_but_never_overwritten(session):
    header = HEADER + ",title"
    run_import(session, csv_bytes(ROW_B + ",", header=header))
    run_import(session, csv_bytes(ROW_B.replace("09:00", "10:00") + ",Primeiro", header=header),
               name="b.csv")
    run_import(session, csv_bytes(ROW_B.replace("09:00", "11:00") + ",Segundo", header=header),
               name="c.csv")
    assert session.scalars(select(SourceItem)).one().title == "Primeiro"


def test_ambiguous_product_name_requires_product_id(session):
    session.add_all([ProductCandidate(name="Pet hair remover"),
                     ProductCandidate(name="Pet hair remover")])
    session.commit()
    result = run_import(session, csv_bytes(ROW_A))
    assert result.rows_rejected == 1 and "vários candidatos" in result.errors[0].message

    target = session.scalars(select(ProductCandidate).order_by(ProductCandidate.id)).all()[1]
    header = HEADER + ",product_id"
    ok = run_import(session, csv_bytes(f"{ROW_A},{target.id}", header=header), name="b.csv")
    assert ok.rows_accepted == 1
    assert session.scalars(select(ProductSource)).one().product_id == target.id

    wrong = run_import(session, csv_bytes(f"{ROW_B},{target.id}", header=header), name="c.csv")
    assert "mas a linha diz 'Lint roller'" in wrong.errors[0].message
    missing = run_import(session, csv_bytes(f"{ROW_B},99999", header=header), name="d.csv")
    assert "99999 não existe" in missing.errors[0].message


def test_similar_names_are_not_merged(session):
    run_import(session, csv_bytes(ROW_A))
    similar = ROW_A.replace("Pet hair remover", "Pet hair remover roller")
    similar = similar.replace("/ad/123", "/ad/124")
    run_import(session, csv_bytes(similar), name="b.csv")
    assert count(session, ProductCandidate) == 2


@pytest.mark.parametrize("data,fragment", [
    (csv_bytes(ROW_A, header=HEADER + ",view"), "colunas desconhecidas"),
    (csv_bytes(ROW_A, header="product_name,source_url"), "colunas obrigatórias ausentes"),
    (HEADER.encode() + b"\n" + "Café,tiktok_video,https://e.com/x,2026-09-26T12:00:00Z,,,,,"
     .encode("latin-1"), "UTF-8"),
    (b"", "sem cabeçalho"),
    (csv_bytes('"unterminated,tiktok_video,https://e.com/x,2026-09-26T12:00:00Z,,,,,'),
     "CSV malformado"),
])
def test_rejected_file_imports_nothing_and_records_failed_batch(session, data, fragment):
    result = run_import(session, data)
    assert result.status == ImportStatus.FAILED
    assert fragment in result.errors[0].message
    batch = session.get(ImportBatch, result.batch_id)
    assert batch.status == ImportStatus.FAILED and batch.rows_total == 0
    assert fragment in batch.errors[0].error
    assert count(session, Observation) == 0 and count(session, ProductCandidate) == 0


def test_dry_run_writes_nothing(session):
    result = run_import(session, csv_bytes(ROW_A, ROW_B), dry_run=True)
    assert result.rows_accepted == 2 and result.dry_run and result.batch_id is None
    for model in (ImportBatch, Observation, SourceItem, ProductCandidate, ProductSource):
        assert count(session, model) == 0


def test_rejected_dry_run_writes_nothing(session):
    result = run_import(session, csv_bytes(ROW_A, header=HEADER + ",x"), dry_run=True)
    assert result.status == ImportStatus.FAILED and count(session, ImportBatch) == 0


# ---------- comando ----------


def test_cli_file_and_stdin(engine, tmp_path):  # noqa: F811
    path = tmp_path / "lote.csv"
    path.write_bytes(csv_bytes(ROW_A))
    out = io.StringIO()
    assert cli.run([str(path)], engine=engine, stdout=out) == 0
    assert "novas: 1" in out.getvalue() and "Arquivo: lote.csv" in out.getvalue()

    out = io.StringIO()
    code = cli.run(["-", "--name", "stdin.csv"], engine=engine,
                   stdin=io.BytesIO(csv_bytes(ROW_A)), stdout=out)
    assert code == 0 and "duplicadas (já existiam): 1" in out.getvalue()


def test_cli_exit_codes(engine, tmp_path):  # noqa: F811
    bad_row = tmp_path / "bad_row.csv"
    bad_row.write_bytes(csv_bytes(ROW_A, "X,tiktok_video,https://e.com/x,ontem,,,,,"))
    out = io.StringIO()
    assert cli.run([str(bad_row)], engine=engine, stdout=out) == 2
    assert "linha 3:" in out.getvalue()

    bad_file = tmp_path / "bad_file.csv"
    bad_file.write_bytes(csv_bytes(ROW_A, header=HEADER + ",x"))
    out = io.StringIO()
    assert cli.run([str(bad_file)], engine=engine, stdout=out) == 1
    assert "ARQUIVO RECUSADO" in out.getvalue()

    out = io.StringIO()
    assert cli.run([str(tmp_path / "nao_existe.csv")], engine=engine, stdout=out) == 1

    out = io.StringIO()
    assert cli.run([str(bad_row), "--dry-run"], engine=engine, stdout=out) == 2
    assert "SIMULAÇÃO" in out.getvalue()


def test_cli_refuses_concurrent_import_on_postgres(engine, tmp_path):  # noqa: F811
    if engine.dialect.name != "postgresql":
        pytest.skip("trava só existe no PostgreSQL")
    path = tmp_path / "a.csv"
    path.write_bytes(csv_bytes(ROW_A))
    with engine.connect() as other:
        other.execute(text("SELECT pg_advisory_lock(:k)"), {"k": cli.LOCK_KEY})
        out = io.StringIO()
        assert cli.run([str(path)], engine=engine, stdout=out) == 3
        assert "outra importação" in out.getvalue()
        other.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": cli.LOCK_KEY})
    assert cli.run([str(path)], engine=engine, stdout=io.StringIO()) == 0


@pytest.mark.parametrize("query", ["id=2&id=1", "token=%2f&flag&value=%FF", "b=2&a=1"])
def test_url_preserves_identifier_order_and_encoding(query):
    assert normalize_url(f"https://example.com/x?{query}&utm_source=test") == (
        f"https://example.com/x?{query}"
    )


@pytest.mark.parametrize("fragment", ["/product/123", "!/product/123", "?product=123"])
def test_url_preserves_application_route_fragment(fragment):
    url = f"https://example.com/#{fragment}"
    assert normalize_url(url) == url


@pytest.mark.parametrize("url", [
    "https://example.com/x?key=%", "https://example.com/x?key=%G1",
    "https://example.com/x\x00", "https://@example.com/x",
])
def test_url_rejects_invalid_encoding_controls_and_empty_credentials(url):
    with pytest.raises(InvalidURL):
        normalize_url(url)


def test_normalized_name_ambiguity_checks_all_existing_products(session):
    session.add_all([ProductCandidate(name="Pet hair remover"),
                     ProductCandidate(name=" Pet  hair remover ")])
    session.commit()
    result = run_import(session, csv_bytes(ROW_A))
    assert result.rows_rejected == 1
    assert "vários candidatos" in result.errors[0].message
    assert count(session, Observation) == 0


def test_changed_note_is_rejected_without_overwriting_snapshot(session):
    run_import(session, csv_bytes(ROW_A))
    result = run_import(session, csv_bytes(ROW_A.replace("Exemplo ficticio", "Outra nota")))
    assert (result.rows_duplicate, result.rows_rejected) == (0, 1)
    assert "note" in result.errors[0].message
    assert session.scalars(select(Observation)).one().note == "Exemplo ficticio"


def test_conflicting_relation_does_not_change_link_or_create_snapshot(session):
    run_import(session, csv_bytes(ROW_A))
    later = ROW_A.replace("12:00:00", "13:00:00")
    result = run_import(session, csv_bytes(later + ",similar_product", header=HEADER + ",relation"))
    assert result.rows_rejected == 1 and "relation" in result.errors[0].message
    assert session.scalars(select(ProductSource)).one().relation == SourceRelation.SHOWS_PRODUCT
    assert count(session, Observation) == 1


def test_rejected_snapshot_rolls_back_source_metadata_fill(session):
    run_import(session, csv_bytes(ROW_A))
    changed = ROW_A.replace("120000", "999999") + ",Título que não deve entrar"
    result = run_import(session, csv_bytes(changed, header=HEADER + ",title"))
    assert result.rows_rejected == 1
    assert session.scalars(select(SourceItem)).one().title is None


@pytest.mark.parametrize("column,value", [
    ("observed_at", "0001-01-01T00:00:00+14:00"),
    ("observed_at", "9999-12-31T23:59:59-14:00"),
    ("note", "texto\x00original"),
])
def test_unusual_invalid_cell_rejects_only_its_row_and_keeps_raw_data(session, column, value):
    header = HEADER.split(",")
    bad = ROW_A.split(",")
    bad[header.index(column)] = value
    result = run_import(session, csv_bytes(",".join(bad), ROW_B))
    assert (result.rows_accepted, result.rows_rejected) == (1, 1)
    error = session.scalars(select(ImportRowError)).one()
    original = error.raw_row[column]
    if "\x00" in value:
        assert original["encoding"] == "base64-utf8"
        assert base64.b64decode(original["value"]).decode() == value
    else:
        assert original == value


def test_error_and_observation_line_numbers_follow_physical_csv_lines(session):
    data = (HEADER + "\n\n" + ROW_A.replace("Exemplo ficticio", '"Nota\nem duas linhas"')
            + "\n\n" + ROW_B.replace("500", "-1") + "\n").encode()
    result = run_import(session, data)
    assert (result.rows_accepted, result.rows_rejected) == (1, 1)
    assert session.scalars(select(Observation)).one().import_row_number == 3
    assert session.scalars(select(ImportRowError)).one().row_number == 6
    assert result.errors[0].row_number == 6


def test_malformed_tail_rolls_back_all_domain_data_and_counts(session):
    result = run_import(session, csv_bytes(ROW_A, '"unterminated'))
    assert result.status == ImportStatus.FAILED
    assert result.rows_total == result.rows_accepted == 0
    assert count(session, Observation) == count(session, ProductCandidate) == 0
    assert count(session, ImportBatch) == 1


def test_cli_limits_read_before_loading_whole_file(engine, monkeypatch):  # noqa: F811
    monkeypatch.setattr(cli, "MAX_FILE_BYTES", 32)
    source = io.BytesIO(b"x" * 100)
    result = cli.run(["-", "--name", "large.csv", "--dry-run"], engine=engine,
                     stdin=source, stdout=io.StringIO())
    assert result == 1
    assert source.tell() == 33


def test_cli_connection_failure_returns_error_code_and_closes_lock_connection():
    class BrokenConnection:
        invalidated = closed = False

        def execute(self, *args, **kwargs):
            raise OperationalError("lock", None, RuntimeError("unavailable"))

        def invalidate(self):
            self.invalidated = True

        def close(self):
            self.closed = True

    class Engine:
        dialect = type("Dialect", (), {"name": "postgresql"})()

        def connect(self):
            return connection

    connection = BrokenConnection()
    out = io.StringIO()
    result = cli.run(["-", "--name", "a.csv"], engine=Engine(),
                     stdin=io.BytesIO(csv_bytes(ROW_A)), stdout=out)
    assert result == 1 and "falha" in out.getvalue()
    assert connection.closed and connection.invalidated


def test_legacy_url_normalization_is_flagged_without_duplicating_source(session):
    legacy_url = "https://example.com/x?a=1&b=2"
    session.add(SourceItem(source_type=SourceType.TIKTOK_TOP_ADS, external_url=legacy_url))
    session.commit()
    result = run_import(session, csv_bytes(
        ROW_A.replace("https://example.com/ad/123", "https://example.com/x?b=2&a=1")
    ))
    assert result.rows_rejected == 1 and "normalização antiga" in result.errors[0].message
    assert count(session, SourceItem) == 1
    assert count(session, Observation) == count(session, ProductCandidate) == 0


@pytest.mark.parametrize("column", ["views", "observed_price_usd"])
def test_numbers_require_ascii_digits(column):
    with pytest.raises(RowError):
        parse_row(raw_row(**{column: "١٢"}), now=NOW)


def test_release_lock_failure_discards_connection_before_returning_to_pool():
    conn = MagicMock()
    conn.execute.side_effect = OperationalError("unlock", None, RuntimeError("unavailable"))
    cli._release_lock(conn)
    conn.invalidate.assert_called_once()
    conn.close.assert_called_once()


def test_cli_read_failure_is_reported_without_accessing_database():
    source = MagicMock()
    source.read.side_effect = OSError("unreadable")
    database_engine = MagicMock()
    out = io.StringIO()
    assert cli.run(
        ["-", "--name", "a.csv"], engine=database_engine, stdin=source, stdout=out
    ) == 1
    database_engine.connect.assert_not_called()
    assert "falha ao ler" in out.getvalue()
