# Настройки из переменных окружения
"""Application configuration loaded from environment variables."""
from pydantic_settings import BaseSettings, SettingsConfigDict


# читает DATABASE_URL и прочее из окружения
class Settings(BaseSettings):
    """Runtime settings.

    Values are read from environment variables (case-insensitive) or an
    optional ``.env`` file. ``DATABASE_URL`` must use an async driver, e.g.
    ``postgresql+asyncpg://user:pass@host:5432/db``.
    """

    database_url: str = (
        "postgresql+asyncpg://wallet:wallet@localhost:5432/wallet"
    )
    echo_sql: bool = False

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
