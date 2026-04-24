"""Application settings loaded from environment variables."""

from pathlib import Path

from pydantic_settings import BaseSettings

# Fixed config directory path; sessions and .env live here
CONFIG_DIR = Path(__file__).resolve().parent
ENV_FILE = CONFIG_DIR / ".env"


class Settings(BaseSettings):
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

    # yt-dlp
    ytdlp_format: str = "bestvideo+bestaudio/best"
    ytdlp_use_aria2: bool = True

    # TG parallel download
    tg_parallel_connections: int = 4         # per-file concurrent chunk streams
    tg_parallel_threshold: int = 10          # MB; files below this use single stream

    # Notification
    tg_notify_on_complete: bool = True       # reply in chat when download finishes
    tg_notify_on_fail: bool = True           # reply in chat when download fails

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

    class Config:
        env_file = str(ENV_FILE)
        env_file_encoding = "utf-8"


def reload_settings() -> "Settings":
    """Re-read .env and return a fresh Settings instance."""
    global settings
    settings = Settings()
    return settings


settings = Settings()
