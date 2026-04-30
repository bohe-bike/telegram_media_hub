"""Single-stream Telegram media downloader.

Uses Pyrogram's built-in ``download_media()`` exclusively — one TCP
connection, one sequential stream.  Keeps the worker architecture simple
and avoids the auth-key edge cases that multi-session parallel downloads
introduced.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from loguru import logger
from pyrogram import Client


async def download_tg_file(
    client: Client,
    file_id_str: str,
    file_size: int,
    dest_path: Path,
    progress: Callable[[int, int], None] | None = None,
) -> Path:
    """Download a Telegram file to *dest_path* using one Pyrogram stream."""
    logger.info(
        f"Using single-stream TG download for {dest_path.name} "
        f"({file_size or 'unknown'} bytes)"
    )
    downloaded = await client.download_media(
        message=file_id_str,
        file_name=str(dest_path),
        progress=progress,
    )

    if downloaded and os.path.exists(downloaded):
        dl = Path(downloaded)
        if dl != dest_path:
            dl.rename(dest_path)
        return dest_path

    raise RuntimeError("Pyrogram download_media returned no file")
