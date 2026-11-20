"""Project settings, loaded from environment variables and an optional `.env` file."""

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DATASET_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    raw_dir: Path = Path("data/raw")
    processed_dir: Path = Path("data/processed")
    name: str = "my_dataset"
    seed: int = 42
    log_level: str = "INFO"
    hf_token: SecretStr | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
