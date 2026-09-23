from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="LEADERBOARD_",
        extra="ignore",
        hide_input_in_errors=True,
    )

    app_name: str = "Gaming Leaderboard"
    environment: str = "development"
    submission_api_key: SecretStr | None = Field(default=None, min_length=32, max_length=256)
    redis_url: str = "redis://localhost:6379/0"
    redis_key_prefix: str = Field(default="leaderboard", min_length=1, max_length=128)
    redis_socket_timeout: float = Field(default=2.0, gt=0, le=30)
    redis_connect_timeout: float = Field(default=2.0, gt=0, le=30)


    @field_validator("submission_api_key")
    @classmethod
    def validate_submission_api_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            secret = value.get_secret_value()
            if any(not 33 <= ord(character) <= 126 for character in secret):
                raise ValueError(
                    "Submission API key must contain printable ASCII characters without whitespace"
                )
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
