"""Rotas de candidatos, fontes e observações — mesmas regras da importação CSV."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from product_intelligence.db.models import Observation, Review, SourceItem
from product_intelligence.db.session import get_engine
from product_intelligence.ingest.importer import import_csv
from product_intelligence.main import create_app
from tests.test_models import engine  # noqa: F401 — fixture: banco migrado (SQLite/PostgreSQL)

T0 = "2026-09-26T12:00:00Z"
URL = "https://www.tiktok.com/@maker/video/123"


@pytest.fixture
def client(engine):  # noqa: F811
    app = create_app()
    app.dependency_overrides[get_engine] = lambda: engine
    with TestClient(app) as c:
        yield c


def detail(resp) -> str:
    return str(resp.json()["detail"])


def new_candidate(client, name="Pet hair remover", **extra) -> dict:
    resp = client.post("/candidates", json={"name": name, **extra})
    assert resp.status_code == 201, resp.text
    return resp.json()


def new_source(client, url=URL, source_type="tiktok_video", **extra) -> dict:
    resp = client.post("/sources", json={"source_type": source_type, "url": url, **extra})
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


def observe(client, source_id, **body):
    body.setdefault("observed_at", T0)
    return client.post(f"/sources/{source_id}/observations", json=body)


# ---------- candidatos ----------


def test_candidate_crud(client):
    created = new_candidate(client, "  Pet   hair remover ", category="Pets")
    assert created["name"] == "Pet hair remover"  # espaços normalizados como no CSV
    assert created["status"] == "new" and created["category"] == "Pets"
    assert created["created_at"].endswith("Z") or "+00:00" in created["created_at"]

    cid = created["id"]
    got = client.get(f"/candidates/{cid}").json()
    assert got["sources"] == [] and got["name"] == "Pet hair remover"

    patched = client.patch(f"/candidates/{cid}", json={"description": "rolo adesivo",
                                                       "category": None})
    assert patched.status_code == 200
    assert patched.json()["description"] == "rolo adesivo" and patched.json()["category"] is None

    assert client.delete(f"/candidates/{cid}").status_code == 204
    assert client.get(f"/candidates/{cid}").status_code == 404


def test_candidate_name_must_be_unique_like_csv_matching(client):
    first = new_candidate(client, "Pet hair remover")
    for variant in ("pet HAIR remover", "Pet  hair   remover"):
        resp = client.post("/candidates", json={"name": variant})
        assert resp.status_code == 409 and f"ids {first['id']}" in detail(resp)
    other = new_candidate(client, "Pet hair remover roller")  # parecido não é igual
    resp = client.patch(f"/candidates/{other['id']}", json={"name": "PET hair remover"})
    assert resp.status_code == 409
    # Renomear para o próprio nome (outra caixa) é permitido.
    assert client.patch(f"/candidates/{first['id']}",
                        json={"name": "Pet Hair Remover"}).status_code == 200


@pytest.mark.parametrize("body", [
    {}, {"name": ""}, {"name": "   "}, {"name": "x" * 201}, {"name": "a\x00b"},
    {"name": "ok", "status": "test"},  # status muda por review, não por aqui
    {"name": "ok", "unknown": 1},
    {"name": "ok", "category": "x" * 101},
])
def test_candidate_validation(client, body):
    assert client.post("/candidates", json=body).status_code == 422


def test_candidate_patch_rejects_null_name_and_status(client):
    cid = new_candidate(client)["id"]
    assert client.patch(f"/candidates/{cid}", json={"name": None}).status_code == 422
    assert client.patch(f"/candidates/{cid}", json={"status": "test"}).status_code == 422
    assert client.patch("/candidates/9999", json={"description": "x"}).status_code == 404


def test_candidate_list_filters_and_pagination(client):
    for name in ("Alpha 100%", "Beta", "alpha_2", "Gamma"):
        new_candidate(client, name)
    page = client.get("/candidates", params={"limit": 2}).json()
    assert page["total"] == 4 and len(page["items"]) == 2 and page["limit"] == 2
    assert client.get("/candidates", params={"offset": 3}).json()["items"][0]["name"] == "Gamma"
    names = [c["name"] for c in client.get("/candidates", params={"q": "ALPHA"}).json()["items"]]
    assert names == ["Alpha 100%", "alpha_2"]
    # % e _ são literais na busca, não curingas.
    assert client.get("/candidates", params={"q": "0%"}).json()["total"] == 1
    assert client.get("/candidates", params={"q": "a_"}).json()["total"] == 1
    assert client.get("/candidates", params={"status": "new"}).json()["total"] == 4
    assert client.get("/candidates", params={"status": "watch"}).json()["total"] == 0
    assert client.get("/candidates", params={"limit": 0}).status_code == 422
    assert client.get("/candidates", params={"limit": 201}).status_code == 422
    assert client.get("/candidates", params={"status": "bogus"}).status_code == 422


def test_candidate_with_review_cannot_be_deleted(client, engine):  # noqa: F811
    cid = new_candidate(client)["id"]
    with Session(engine) as s:
        s.add(Review(product_id=cid, decision="watch", reason="evidência inicial"))
        s.commit()
    resp = client.delete(f"/candidates/{cid}")
    assert resp.status_code == 409 and "histórico" in detail(resp)
    assert client.get(f"/candidates/{cid}").status_code == 200


# ---------- fontes ----------


def test_source_create_is_idempotent_by_normalized_url(client):
    first = client.post("/sources", json={"source_type": "tiktok_video",
                                          "url": URL + "?utm_source=x#frag"})
    assert first.status_code == 201 and first.json()["created"] is True
    assert first.json()["external_url"] == URL
    again = client.post("/sources", json={"source_type": "tiktok_video",
                                          "url": "HTTPS://WWW.TIKTOK.COM/@maker/video/123",
                                          "title": "Demo"})
    assert again.status_code == 200 and again.json()["created"] is False
    assert again.json()["id"] == first.json()["id"]
    assert again.json()["title"] == "Demo"  # campo vazio completado
    third = new_source(client, title="Outro título")
    assert third["title"] == "Demo"  # nunca sobrescreve pela criação
    # Mesmo link com outro tipo é outra fonte.
    assert new_source(client, source_type="other")["id"] != first.json()["id"]


def test_source_url_keeps_query_order_and_app_routes(client):
    a = new_source(client, "https://example.com/p?id=2&id=1")
    b = new_source(client, "https://example.com/p?id=1&id=2")
    assert a["id"] != b["id"]  # ordem de parâmetros repetidos pode importar
    route = new_source(client, "https://example.com/app#/item/9")
    assert route["external_url"] == "https://example.com/app#/item/9"


def test_source_blocked_by_legacy_normalization(client, engine):  # noqa: F811
    with Session(engine) as s:  # fonte gravada pela normalização antiga (query ordenada)
        s.add(SourceItem(source_type="other", external_url="https://example.com/p?a=1&b=2"))
        s.commit()
    resp = client.post("/sources", json={"source_type": "other",
                                         "url": "https://example.com/p?b=2&a=1"})
    assert resp.status_code == 409 and "normalização antiga" in detail(resp)


@pytest.mark.parametrize("body", [
    {"source_type": "instagram", "url": URL},
    {"source_type": "tiktok_video", "url": "tiktok.com/x"},
    {"source_type": "tiktok_video", "url": "ftp://example.com/x"},
    {"source_type": "tiktok_video"},
    {"source_type": "tiktok_video", "url": URL, "published_at": "2026-09-20T00:00:00"},
    {"source_type": "tiktok_video", "url": URL, "published_at": "2999-01-01T00:00:00Z"},
    {"source_type": "tiktok_video", "url": URL, "title": "x" * 501},
    {"source_type": "tiktok_video", "url": URL, "views": 10},
])
def test_source_validation(client, body):
    assert client.post("/sources", json=body).status_code == 422


def test_source_detail_update_delete(client):
    source = new_source(client, published_at="2026-09-20T09:00:00-03:00")
    assert source["published_at"].startswith("2026-09-20T12:00:00")
    sid = source["id"]

    resp = client.patch(f"/sources/{sid}", json={"title": "Corrigido", "creator_handle": "@m"})
    assert resp.status_code == 200 and resp.json()["title"] == "Corrigido"
    assert client.patch(f"/sources/{sid}", json={"url": "https://x.com/y"}).status_code == 422
    assert client.patch(f"/sources/{sid}", json={"source_type": "other"}).status_code == 422

    got = client.get(f"/sources/{sid}").json()
    assert got["observation_count"] == 0 and got["last_observed_at"] is None
    assert got["candidates"] == []

    assert client.delete(f"/sources/{sid}").status_code == 204
    assert client.get(f"/sources/{sid}").status_code == 404


def test_source_with_observations_cannot_be_deleted(client):
    sid = new_source(client)["id"]
    assert observe(client, sid, views=10).status_code == 201
    resp = client.delete(f"/sources/{sid}")
    assert resp.status_code == 409 and "observações" in detail(resp)
    assert client.get(f"/sources/{sid}").status_code == 200


def test_source_list_filters(client):
    cid = new_candidate(client)["id"]
    s1 = new_source(client, "https://example.com/1")
    new_source(client, "https://example.com/2", source_type="other")
    client.post(f"/candidates/{cid}/sources", json={"source_id": s1["id"]})
    assert client.get("/sources").json()["total"] == 2
    assert client.get("/sources", params={"source_type": "other"}).json()["total"] == 1
    linked = client.get("/sources", params={"candidate_id": cid}).json()
    assert [s["id"] for s in linked["items"]] == [s1["id"]]


# ---------- associações ----------


def test_link_by_url_creates_source_and_is_idempotent(client):
    cid = new_candidate(client)["id"]
    body = {"source_type": "tiktok_video", "url": URL, "title": "Demo"}
    first = client.post(f"/candidates/{cid}/sources", json=body)
    assert first.status_code == 201
    assert first.json()["source_created"] and first.json()["link_created"]
    assert first.json()["relation"] == "shows_product"
    again = client.post(f"/candidates/{cid}/sources", json=body)
    assert again.status_code == 200
    assert not again.json()["source_created"] and not again.json()["link_created"]

    other = client.post(f"/candidates/{cid}/sources",
                        json={**body, "relation": "similar_product"})
    assert other.status_code == 409 and "relação diferente" in detail(other)

    got = client.get(f"/candidates/{cid}").json()
    assert len(got["sources"]) == 1 and got["sources"][0]["source"]["title"] == "Demo"
    src = client.get(f"/sources/{got['sources'][0]['source']['id']}").json()
    assert src["candidates"][0]["candidate"]["id"] == cid


@pytest.mark.parametrize("body", [
    {},
    {"source_id": 1, "source_type": "other", "url": "https://e.com/x"},
    {"source_type": "other"},
    {"url": "https://e.com/x"},
    {"source_id": 1, "title": "x"},
    {"source_id": 0},
    {"source_type": "other", "url": "https://e.com/x", "relation": "same"},
])
def test_link_validation(client, body):
    cid = new_candidate(client)["id"]
    assert client.post(f"/candidates/{cid}/sources", json=body).status_code == 422


def test_link_not_found(client):
    cid = new_candidate(client)["id"]
    assert client.post(f"/candidates/{cid}/sources", json={"source_id": 999}).status_code == 404
    assert client.post("/candidates/999/sources",
                       json={"source_type": "other", "url": URL}).status_code == 404
    # Candidato inexistente não deixa fonte órfã criada.
    assert client.get("/sources").json()["total"] == 0


def test_link_review_and_unlink(client):
    cid = new_candidate(client)["id"]
    sid = new_source(client)["id"]
    client.post(f"/candidates/{cid}/sources", json={"source_id": sid})

    resp = client.patch(f"/candidates/{cid}/sources/{sid}",
                        json={"relation": "similar_product", "reviewed": True})
    assert resp.status_code == 200
    assert resp.json()["relation"] == "similar_product" and resp.json()["reviewed_at"]
    resp = client.patch(f"/candidates/{cid}/sources/{sid}", json={"reviewed": False})
    assert resp.json()["reviewed_at"] is None
    assert client.patch(f"/candidates/{cid}/sources/{sid}",
                        json={"relation": None}).status_code == 422
    assert client.patch(f"/candidates/{cid}/sources/999", json={}).status_code == 404

    observe(client, sid, views=5)
    assert client.delete(f"/candidates/{cid}/sources/{sid}").status_code == 204
    assert client.get(f"/candidates/{cid}").json()["sources"] == []
    # A fonte e o histórico permanecem.
    assert client.get(f"/sources/{sid}").json()["observation_count"] == 1
    assert client.delete(f"/candidates/{cid}/sources/{sid}").status_code == 404


def test_deleting_candidate_keeps_sources_and_observations(client):
    cid = new_candidate(client)["id"]
    sid = new_source(client)["id"]
    client.post(f"/candidates/{cid}/sources", json={"source_id": sid})
    observe(client, sid, views=1)
    assert client.delete(f"/candidates/{cid}").status_code == 204
    src = client.get(f"/sources/{sid}").json()
    assert src["candidates"] == [] and src["observation_count"] == 1


# ---------- observações ----------


def test_observation_create_keeps_unknowns_null(client):
    sid = new_source(client)["id"]
    resp = observe(client, sid, observed_at="2026-09-26T09:00:00-03:00", views=120000,
                   market_region="us", region_basis="source_filter_us",
                   observed_price_usd="19.90", note="  anotação  ")
    assert resp.status_code == 201, resp.text
    obs = resp.json()
    assert obs["created"] is True and obs["capture_method"] == "manual"
    assert obs["observed_at"].startswith("2026-09-26T12:00:00")
    assert obs["market_region"] == "US" and obs["views"] == 120000
    assert obs["likes"] is None and obs["comments_count"] is None and obs["shares"] is None
    assert obs["observed_price_usd"] == "19.90" and obs["note"] == "anotação"
    assert obs["import_batch_id"] is None and obs["import_row_number"] is None
    assert client.get(f"/observations/{obs['id']}").json()["views"] == 120000

    minimal = observe(client, sid, observed_at="2026-09-26T13:00:00Z").json()
    assert minimal["region_basis"] == "unknown" and minimal["views"] is None


def test_observation_duplicate_and_conflict(client, engine):  # noqa: F811
    sid = new_source(client)["id"]
    first = observe(client, sid, views=10, likes=1)
    assert first.status_code == 201
    same_instant = observe(client, sid, observed_at="2026-09-26T09:00:00-03:00", views=10,
                           likes=1)
    assert same_instant.status_code == 200 and same_instant.json()["created"] is False
    assert same_instant.json()["id"] == first.json()["id"]

    conflict = observe(client, sid, views=11, likes=1)
    assert conflict.status_code == 409 and "valores diferentes (views)" in detail(conflict)
    unknown_vs_zero = observe(client, sid, views=10, likes=1, shares=0)
    assert unknown_vs_zero.status_code == 409 and "shares" in detail(unknown_vs_zero)
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(Observation)) == 1


@pytest.mark.parametrize("body,fragment", [
    ({"observed_at": "2026-09-26T12:00:00"}, "sem fuso"),
    ({"observed_at": "26/09/2026"}, "ISO 8601"),
    ({"observed_at": 1790000000}, "ISO 8601"),
    ({"views": "1.2K"}, "views"),
    ({"views": "1,200"}, "views"),
    ({"views": -5}, "views"),
    ({"views": 1.5}, "views"),
    ({"views": True}, "views"),
    ({"views": 2**63}, "grande demais"),
    ({"observed_price_usd": "$19.99"}, "observed_price_usd"),
    ({"observed_price_usd": 19.999}, "observed_price_usd"),
    ({"observed_price_usd": -1}, "observed_price_usd"),
    ({"market_region": "USA"}, "market_region"),
    ({"market_region": "U1"}, "market_region"),
    ({"region_basis": "source_filter_us"}, "exige market_region=US"),
    ({"region_basis": "source_filter_us", "market_region": "BR"}, "exige market_region=US"),
    ({"capture_method": "csv"}, "extra"),
    ({"import_batch_id": 1}, "extra"),
    ({"note": "a\x00b"}, "NUL"),
])
def test_observation_validation_matches_csv(client, body, fragment):
    sid = new_source(client)["id"]
    body = {"observed_at": T0, **body}
    resp = client.post(f"/sources/{sid}/observations", json=body)
    assert resp.status_code == 422
    assert fragment.lower() in resp.text.lower()


def test_observation_in_future_rejected(client):
    sid = new_source(client)["id"]
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    resp = observe(client, sid, observed_at=future)
    assert resp.status_code == 422 and "futuro" in resp.text


def test_observations_are_create_only(client):
    sid = new_source(client)["id"]
    oid = observe(client, sid, views=1).json()["id"]
    assert client.patch(f"/observations/{oid}", json={"views": 2}).status_code == 405
    assert client.put(f"/observations/{oid}", json={"views": 2}).status_code == 405
    assert client.delete(f"/observations/{oid}").status_code == 405
    assert client.get(f"/observations/{oid}").json()["views"] == 1
    assert client.get("/observations/999").status_code == 404
    assert observe(client, 999).status_code == 404


def test_observation_list_is_chronological(client):
    sid = new_source(client)["id"]
    for hour, views in ((15, 30), (9, 10), (12, 20)):
        observe(client, sid, observed_at=f"2026-09-26T{hour:02d}:00:00Z", views=views)
    page = client.get(f"/sources/{sid}/observations").json()
    assert [o["views"] for o in page["items"]] == [10, 20, 30] and page["total"] == 3
    page = client.get(f"/sources/{sid}/observations", params={"limit": 1, "offset": 1}).json()
    assert [o["views"] for o in page["items"]] == [20]
    detail_ = client.get(f"/sources/{sid}").json()
    assert detail_["observation_count"] == 3
    assert detail_["last_observed_at"].startswith("2026-09-26T15:00:00")
    assert client.get("/sources/999/observations").status_code == 404


# ---------- API e CSV chegam ao mesmo lugar ----------


def test_api_and_csv_share_the_same_records(client, engine):  # noqa: F811
    cid = new_candidate(client, "Pet hair remover")["id"]
    client.post(f"/candidates/{cid}/sources",
                json={"source_type": "tiktok_top_ads", "url": "https://example.com/ad/123"})
    sid = client.get(f"/candidates/{cid}").json()["sources"][0]["source"]["id"]
    observe(client, sid, views=120000, likes=4300, market_region="US",
            region_basis="source_filter_us", note="Exemplo ficticio")

    csv_data = (
        "product_name,source_type,source_url,observed_at,market_region,region_basis,"
        "views,likes,note\n"
        "pet  HAIR remover,tiktok_top_ads,https://EXAMPLE.com/ad/123?utm_source=x,"
        "2026-09-26T09:00:00-03:00,us,source_filter_us,120000,4300,Exemplo ficticio\n"
        "Pet hair remover,tiktok_top_ads,https://example.com/ad/123,"
        "2026-09-26T18:00:00Z,US,source_filter_us,150000,5000,\n"
    ).encode()
    with Session(engine) as s:
        result = import_csv(s, csv_data, "lote.csv",
                            now=datetime(2026, 9, 27, tzinfo=UTC))
    # Linha 1 = o snapshot já registrado pela API; linha 2 = snapshot novo.
    assert (result.rows_accepted, result.rows_duplicate, result.rows_rejected) == (1, 1, 0)
    assert (result.products_created, result.sources_created) == (0, 0)

    page = client.get(f"/sources/{sid}/observations").json()
    assert [(o["views"], o["capture_method"]) for o in page["items"]] == [
        (120000, "manual"), (150000, "csv")]

    # E a API reconhece o que o CSV criou.
    resp = client.post("/candidates", json={"name": "PET hair REMOVER"})
    assert resp.status_code == 409


def test_openapi_lists_routes(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert "patch" not in paths["/observations/{observation_id}"]
    assert set(paths["/sources/{source_id}/observations"]) == {"get", "post"}
    with Session(client.app.dependency_overrides[get_engine]()) as s:
        assert s.execute(text("SELECT 1")).scalar() == 1
