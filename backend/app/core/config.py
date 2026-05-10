from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "StatBot Pro"
    environment: str = "development"
    database_url: str = "sqlite:///data/db/statbot.db"
    data_dir: Path = Path("data")
    frontend_dist_dir: Path = Path("frontend/dist")
    allowed_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    openai_api_key: str | None = Field(default=None, validation_alias=AliasChoices("OPENAI_API_KEY", "OPENAIAPIKEY"))
    openai_model: str = "gpt-4o-mini"
    agent_temperature: float = 0.0
    max_agent_retries: int = 2
    max_upload_bytes: int = 25 * 1024 * 1024

    sandbox_image: str = "statbot-sandbox:latest"
    sandbox_mode: str = Field(default="volume", description="Use 'volume' for Compose, 'bind' for local Docker paths, or 'local' for single-container hosts.")
    sandbox_local_runner_path: Path = Path("sandbox/runtime/runner.py")
    sandbox_uploads_volume: str = "statbot_uploads"
    sandbox_outputs_volume: str = "statbot_outputs"
    sandbox_memory_limit: str = "512m"
    sandbox_nano_cpus: int = 1_000_000_000
    sandbox_timeout_seconds: int = 25

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def output_dir(self) -> Path:
        return self.data_dir / "outputs"

    @property
    def db_dir(self) -> Path:
        return self.data_dir / "db"

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.allowed_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    settings.db_dir.mkdir(parents=True, exist_ok=True)
    return settings
