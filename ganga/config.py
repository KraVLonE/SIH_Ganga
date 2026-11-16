from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent
DATA_RAW = ROOT / "data" / "raw"
DATA_CURATED = ROOT / "data" / "curated"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GANGA_", env_file=ROOT / ".env", extra="ignore")

    db_user: str = "ganga"
    db_password: str = "ganga"
    db_name: str = "ganga"
    db_host: str = "127.0.0.1"
    db_port: int = 5544
    # Optional: lets the Ask tab answer with Gemini (GANGA_GEMINI_API_KEY).
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"

    @property
    def db_dsn(self) -> str:
        return (
            f"postgresql://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )


settings = Settings()
