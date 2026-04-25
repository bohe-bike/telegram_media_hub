"""Application settings loaded from config/config.toml."""

import tomllib
from pathlib import Path

from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

# Config directory is the top-level "config/" folder at the repository root.
# In Docker: /app/config; locally: <project>/config.
# Since this file lives in app/core/, we compute the root as two levels up.
APP_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_DIR = APP_ROOT / "config"
TOML_FILE = CONFIG_DIR / "config.toml"


class _FlatTomlSource(PydanticBaseSettingsSource):
    """Settings source that reads config.toml and flattens all sections.

    Supports both flat TOML (key = value) and sectioned TOML
    ([section] / key = value).  All keys from all sections are merged
    into one flat dict so the pydantic model stays flat.
    """

    def _load(self) -> dict:
        if not TOML_FILE.exists():
            return {}
        with open(TOML_FILE, "rb") as f:
            data = tomllib.load(f)
        flat: dict = {}
        for key, val in data.items():
            if isinstance(val, dict):
                flat.update(val)
            else:
                flat[key] = val
        return flat

    def get_field_value(self, field, field_name):  # type: ignore[override]
        data = self._load()
        return data.get(field_name), field_name, False

    def field_is_complex(self, field) -> bool:  # type: ignore[override]
        return False

    def prepare_field_value(self, field_name, field, value, value_is_complex):  # type: ignore[override]
        return value

    def __call__(self) -> dict:
        return self._load()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # pydantic-settings will ignore env vars; all config comes from TOML
        env_ignore_empty=True,
    )

    # Telegram
    tg_api_id: int = 0
    tg_api_hash: str = ""
    tg_session_name: str = "media_hub"
    tg_monitored_chats: str = ""
    tg_phone_number: str = ""

    # Database
    database_url: str = "postgresql+asyncpg://postgres:postgres@postgres:5432/media_hub"

    # Redis
    redis_url: str = "redis://redis:6379/0"

    # MeTube
    metube_url: str = "http://metube:8081"

    # Storage
    storage_root: str = "/media"
    temp_dir: str = "/media/temp"

    # Workers
    tg_download_workers: int = 3
    external_download_workers: int = 5

    # Retry
    max_retries: int = 5
    retry_base_delay: int = 30  # seconds

    # Proxy
    proxy_pool: str = ""
    proxy_fail_threshold: int = 3    # consecutive failures before marking a proxy FAILED
    proxy_check_interval: int = 300  # seconds between automatic health checks (0 = disabled)

    # TG parallel download
    tg_parallel_connections: int = 4         # per-file concurrent chunk streams
    tg_parallel_threshold: int = 10          # MB; files below this use single stream

    # Notification
    tg_notify_on_complete: bool = True       # reply in chat when download finishes
    tg_notify_on_fail: bool = True           # reply in chat when download fails

    # Web UI authentication (set a non-empty value to enable API key protection)
    api_secret_key: str = ""

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Priority: constructor kwargs > config.toml > model defaults
        return (init_settings, _FlatTomlSource(settings_cls))

    @property
    def monitored_chat_ids(self) -> list[int]:
        if not self.tg_monitored_chats:
            return []
        return [int(x.strip()) for x in self.tg_monitored_chats.split(",") if x.strip()]

    @property
    def proxy_list(self) -> list[str]:
        if not self.proxy_pool:
            return []
        return [x.strip() for x in self.proxy_pool.split(",") if x.strip()]

    @property
    def storage_path(self) -> Path:
        return Path(self.storage_root)

    @property
    def temp_path(self) -> Path:
        return Path(self.temp_dir)

    @property
    def session_dir(self) -> Path:
        """Pyrogram session files are stored in config/sessions/."""
        d = CONFIG_DIR / "sessions"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def tg_proxy(self) -> dict | None:
        """Return the first configured proxy as a Pyrogram-compatible dict.

        Pyrogram expects: {"scheme": "http"/"socks5", "hostname": ..., "port": ...}
        Returns None when no proxies are configured.
        """
        if not self.proxy_list:
            return None
        from urllib.parse import urlparse
        url = self.proxy_list[0]
        parsed = urlparse(url)
        proxy: dict = {
            "scheme": parsed.scheme,
            "hostname": parsed.hostname,
            "port": parsed.port,
        }
        if parsed.username:
            proxy["username"] = parsed.username
        if parsed.password:
            proxy["password"] = parsed.password
        return proxy


def reload_settings() -> "Settings":
    """Re-read config.toml and replace the current settings instance.

    Modules that imported ``settings`` get a proxy reference, so the new
    values are immediately visible everywhere — no restart needed.
    """
    settings._replace(Settings())
    return settings._instance  # type: ignore[attr-defined]


class _SettingsProxy:
    """Module-level singleton that delegates to the current Settings instance.

    ``reload_settings()`` swaps the internal instance; all ``from
    app.core.settings import settings`` sites share this same proxy object,
    so they always see the latest configuration.
    """

    __slots__ = ("_instance",)

    def __init__(self, instance: Settings) -> None:
        self._instance = instance

    def _replace(self, instance: Settings) -> None:
        self._instance = instance

    def __getattr__(self, name: str):
        return getattr(self._instance, name)

    def __setattr__(self, name: str, value) -> None:
        if name == "_instance":
            super().__setattr__(name, value)
        else:
            setattr(self._instance, name, value)


settings = _SettingsProxy(Settings())
