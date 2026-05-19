from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    hik_base_url: str = "http://10.25.1.30"
    hik_hostname: str = "10.25.1.30"
    hik_sid: str | None = None
    hik_encrypted_aes_key: str | None = None
    hik_username: str | None = None
    hik_password: str | None = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
