from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MAGSEARCH_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./data/magsearch.db"
    bundles_dir: Path = Path("./data/bundles")


def get_settings() -> Settings:
    return Settings()
