from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="LEADERBOARD_",
        extra="ignore",
    )

    app_name: str = "Gaming Leaderboard"
    environment: str = "development"
    redis_url: str = "redis://localhost:6379/0"
    redis_key_prefix: str = Field(default="leaderboard", min_length=1, max_length=128)
    redis_socket_timeout: float = Field(default=2.0, gt=0, le=30)
    redis_connect_timeout: float = Field(default=2.0, gt=0, le=30)


@lru_cache
def get_settings() -> Settings:
    return Settings()
