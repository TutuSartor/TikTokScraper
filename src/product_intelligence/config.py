"""Configuração lida de variáveis de ambiente (e opcionalmente do arquivo .env)."""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL, make_url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str | None = None
    postgres_user: str = "pi"
    postgres_password: str = "pi"
    postgres_db: str = "product_intelligence"
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    app_env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"

    @property
    def sqlalchemy_url(self) -> URL:
        """Credenciais separadas evitam interpretar caracteres da senha como parte da URL."""
        if self.database_url is not None:
            return make_url(self.database_url)
        return URL.create(
            "postgresql+psycopg",
            username=self.postgres_user,
            password=self.postgres_password,
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
