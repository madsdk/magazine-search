from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MAGSEARCH_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./data/magsearch.db"
    bundles_dir: Path = Path("./data/bundles")
    session_secret: str = ""
    max_upload_bytes: int = 2 * 1024 * 1024 * 1024  # 2 GB cap on web bundle uploads
    # Auth defaults ON so server deployments are safe out of the box.
    # The desktop launcher flips this to false before importing settings.
    auth_enabled: bool = True


def get_settings() -> Settings:
    return Settings()
