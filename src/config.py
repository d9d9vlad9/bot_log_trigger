from pydantic import Field
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # Telegram Bot
    BOT_TOKEN: str
    ADMIN_IDS: list[int] = Field(default_factory=list)
    ALERT_CHAT_ID: int | None = None

    # База
    DB_PATH: str = Field(default="alerts.db")

    # Интервалы и логирование
    MONITOR_POLL_SECONDS: float = Field(default=0.5)
    AGENT_ACTIVE_THRESHOLD_MINUTES: int = Field(default=10)
    PROGRESS_TIMEOUT_POLL_SECONDS: float = Field(default=30.0)
    LOG_LEVEL: str = Field(default="INFO")

    # Web server (FastAPI / uvicorn)
    API_HOST: str = Field(default="127.0.0.1")
    API_PORT: int = Field(default=8000)
    ALERT_TOKEN: str | None = None

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

settings = Settings()
