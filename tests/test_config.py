from sqlalchemy.engine import make_url

from product_intelligence.config import Settings


def test_separate_credentials_preserve_special_characters():
    settings = Settings(
        _env_file=None,
        database_url=None,
        postgres_user="user@name",
        postgres_password="p@ss:%word/#?$ with spaces",
        postgres_db="product_intelligence",
        postgres_host="db",
    )
    url = make_url(settings.sqlalchemy_url.render_as_string(hide_password=False))
    assert url.username == settings.postgres_user
    assert url.password == settings.postgres_password
    assert url.host == "db"
    assert url.database == "product_intelligence"


def test_explicit_database_url_has_priority():
    settings = Settings(
        _env_file=None,
        database_url="sqlite+pysqlite:///:memory:",
        postgres_password="ignored",
    )
    assert settings.sqlalchemy_url.get_backend_name() == "sqlite"
