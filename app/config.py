"""Application configuration loaded from .env."""

from __future__ import annotations

from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(Path(__file__).parent.parent / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Telegram ---
    tg_bot_token: str
    tg_admin_id: int

    # --- IMAP (Mail.ru) ---
    imap_host: str = "imap.mail.ru"
    imap_port: int = 993
    imap_user: str
    imap_password: str

    # --- YouGile ---
    yougile_api_key: str
    yougile_company_id: str
    yougile_base_url: str = "https://yougile.com/api-v2"

    # --- Obsidian ---
    obsidian_vault_path: str

    # --- Working folder base (YandexDisk) ---
    working_base_path: str = "E:/Работа/YandexDisk"

    # --- LLM ---
    llm_api_key: str = ""
    llm_base_url: str = "https://integrate.api.nvidia.com/v1"
    llm_model: str = "meta/llama-3.3-70b-instruct"

    # --- Server ---
    web_host: str = "127.0.0.1"
    web_port: int = 8080

    # --- Owner mapping ---
    owner_mapping_raw: str = ""

    @property
    def owner_mapping(self) -> dict[str, str]:
        """Parse 'ЧМ:KK,СРБ:KK,...' into {'ЧМ': 'KK', ...}.

        Falls back to auto-scan from vault if empty.
        """
        mapping: dict[str, str] = {}
        raw = self.owner_mapping_raw.strip()
        if raw:
            for pair in raw.split(","):
                if ":" in pair:
                    k, v = pair.split(":", 1)
                    mapping[k.strip()] = v.strip()
        return mapping

    @property
    def vault_path(self) -> Path:
        return Path(self.obsidian_vault_path)


settings = Settings()  # type: ignore[call-arg]
