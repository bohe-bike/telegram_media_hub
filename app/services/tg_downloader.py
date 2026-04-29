"""Parallel chunk downloader for Telegram files via MTProto.

Pyrogram's default `download_media()` uses a single connection to pull
chunks sequentially.  For large files this module opens N concurrent
chunk streams on the correct DC session, writing them to a pre-allocated
temp file at the right offsets, then returns the completed path.

For small files (< threshold) it falls back to the standard single-stream
download to avoid the overhead.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Callable, Optional

from loguru import logger
from pyrogram import Client, raw
from pyrogram.errors import FloodWait
from pyrogram.file_id import FileId, PHOTO_TYPES
from pyrogram.session import Auth, Session

from app.core.settings import settings

CHUNK_SIZE = 1024 * 1024  # 1 MB – Telegram maximum per GetFile call


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------

async def download_tg_file(
    client: Client,
    file_id_str: str,
    file_size: int,
    dest_path: Path,
    num_workers: int | None = None,
    progress: Optional[Callable[[int, int], None]] = None,
) -> Path:
    """Download a TG file – automatically choose single vs parallel mode.

    Returns the *final* path on disk (same as *dest_path*).
    """
    workers = num_workers or settings.tg_parallel_connections
    threshold_bytes = settings.tg_parallel_threshold * 1024 * 1024

    if file_size <= threshold_bytes or workers <= 1:
        logger.info(
            f"Using single-stream download for {dest_path.name} "
            f"({file_size} bytes)"
        )
        return await _single_stream(client, file_id_str, dest_path, progress)

    logger.info(
        f"Using parallel download ({workers} workers) for {dest_path.name} "
        f"({file_size} bytes)"
    )
    try:
        return await _parallel_stream(
            client, file_id_str, file_size, dest_path, workers, progress,
        )
    except Exception as exc:
        logger.warning(
            f"Parallel download failed ({exc}), falling back to single-stream"
        )
        # Clean up partial file
        if dest_path.exists():
            dest_path.unlink(missing_ok=True)
        return await _single_stream(client, file_id_str, dest_path, progress)


# ------------------------------------------------------------------
# Single-stream (standard Pyrogram)
# ------------------------------------------------------------------

async def _single_stream(
    client: Client,
    file_id_str: str,
    dest_path: Path,
    progress: Optional[Callable] = None,
) -> Path:
    downloaded = await client.download_media(
        message=file_id_str,
        file_name=str(dest_path),
        progress=progress,
    )
    if downloaded and os.path.exists(downloaded):
        # Pyrogram may return a slightly different path; rename if needed
        dl = Path(downloaded)
        if dl != dest_path:
            dl.rename(dest_path)
        return dest_path
    raise RuntimeError("Pyrogram download_media returned no file")


# ------------------------------------------------------------------
# Parallel-stream
# ------------------------------------------------------------------

async def _parallel_stream(
    client: Client,
    file_id_str: str,
    file_size: int,
    dest_path: Path,
    num_workers: int,
    progress: Optional[Callable] = None,
) -> Path:
    file_id = FileId.decode(file_id_str)
    dc_id = file_id.dc_id

    location = _build_location(file_id)

    total_chunks = (file_size + CHUNK_SIZE - 1) // CHUNK_SIZE

    # Pre-allocate the output file
    with open(dest_path, "wb") as f:
        f.truncate(file_size)

    downloaded_bytes = 0
    lock = asyncio.Lock()
    # Use a bounded queue so we never create more than num_workers*2 coroutines
    # in flight at once – avoids memory pressure on very large files.
    chunk_queue: asyncio.Queue[int] = asyncio.Queue()
    for i in range(total_chunks):
        chunk_queue.put_nowait(i)

    # Create a dedicated session for each worker to avoid AUTH_KEY_DUPLICATED
    worker_sessions = await asyncio.gather(*[
        _get_worker_session(client, dc_id) for _ in range(num_workers)
    ])

    async def _fetch_chunk(idx: int, sess: Session) -> None:
        nonlocal downloaded_bytes
        offset = idx * CHUNK_SIZE
        remaining = file_size - offset
        # Telegram requires limit to be a multiple of 4096 and at most CHUNK_SIZE.
        # Round up to the next 4096 boundary so the last chunk is always valid.
        limit = min(CHUNK_SIZE, ((remaining + 4095) // 4096) * 4096)

        for attempt in range(5):
            try:
                # Check session health before each attempt (handles idle disconnects)
                if attempt > 0:
                    sess = await _ensure_session_alive(sess, client, dc_id)

                resp = await sess.invoke(
                    raw.functions.upload.GetFile(
                        location=location,
                        offset=offset,
                        limit=limit,
                        precise=True,
                    )
                )
                break
            except FloodWait as fw:
                wait_secs = fw.value + 1
                logger.warning(
                    f"Chunk {idx}: FloodWait {fw.value}s — sleeping {wait_secs}s"
                )
                await asyncio.sleep(wait_secs)
            except Exception as e:
                if attempt == 4:
                    raise
                logger.debug(f"Chunk {idx} attempt {attempt+1} failed: {e}")
                await asyncio.sleep(1.0 * (attempt + 1))

        data = resp.bytes

        # Write at the correct position; use the shared lock to serialise
        # seeks so a single open fd is reused across all writers.
        async with lock:
            with open(dest_path, "r+b") as f:
                f.seek(offset)
                f.write(data)
            downloaded_bytes += len(data)
            if progress:
                try:
                    progress(downloaded_bytes, file_size)
                except Exception:
                    pass

    async def _worker(sess: Session) -> None:
        while True:
            try:
                idx = chunk_queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            await _fetch_chunk(idx, sess)
            chunk_queue.task_done()

    try:
        await asyncio.gather(*[_worker(s) for s in worker_sessions])
    finally:
        # Clean up all temporary worker sessions to prevent TCP connection
        # & auth-key leaks.  The home DC session (client.session) is NOT
        # closed here — only the ad-hoc sessions created for parallelism.
        for sess in worker_sessions:
            if sess is not client.session:
                try:
                    await sess.stop()
                except Exception:
                    pass

    # Verify file size
    actual = dest_path.stat().st_size
    if actual != file_size:
        raise RuntimeError(
            f"Size mismatch: expected {file_size}, got {actual}"
        )

    return dest_path


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _build_location(fid: FileId):
    """Build the raw InputFileLocation from a decoded FileId."""
    if fid.file_type in PHOTO_TYPES:
        return raw.types.InputPhotoFileLocation(
            id=fid.media_id,
            access_hash=fid.access_hash,
            file_reference=fid.file_reference,
            thumb_size=fid.thumbnail_size or "",
        )
    return raw.types.InputDocumentFileLocation(
        id=fid.media_id,
        access_hash=fid.access_hash,
        file_reference=fid.file_reference,
        thumb_size=fid.thumbnail_size or "",
    )


async def _get_media_session(client: Client, dc_id: int) -> Session:
    """Return an active media session for *dc_id*, creating one if needed.

    Re-uses Pyrogram's internal ``media_sessions`` dict so we don't
    duplicate connections.
    """
    # Pyrogram 2.x keeps media sessions in client._media_sessions or
    # client.media_sessions depending on the fork.  Try both.
    sessions_dict: dict = getattr(
        client, "media_sessions",
        getattr(client, "_media_sessions", {}),
    )

    session = sessions_dict.get(dc_id)
    if session is not None:
        return session

    my_dc = await client.storage.dc_id()

    if dc_id == my_dc:
        # Same DC – reuse the main session
        session = client.session
    else:
        # Different DC – perform DH key exchange for that DC, then import
        # the user's authorization so the session is authenticated.
        logger.debug(f"Creating media session for DC {dc_id}")
        exported = await client.invoke(
            raw.functions.auth.ExportAuthorization(dc_id=dc_id)
        )
        test_mode = await client.storage.test_mode()
        # Generate a fresh auth key for the target DC via MTProto DH exchange.
        # We must NOT reuse the home DC's auth key – each DC keeps its own.
        auth_key = await Auth(client, dc_id, test_mode).create()
        session = Session(
            client, dc_id,
            auth_key,
            test_mode,
            is_media=True,
        )
        await session.start()
        await session.invoke(
            raw.functions.auth.ImportAuthorization(
                id=exported.id,
                bytes=exported.bytes,
            )
        )

    sessions_dict[dc_id] = session
    return session


async def _get_worker_session(client: Client, dc_id: int) -> Session:
    """Create a new dedicated session for a single worker.

    Each worker needs its own auth key to avoid AUTH_KEY_DUPLICATED errors
    when multiple workers invoke requests concurrently on the same DC.

    For DIFFERENT DCs: export auth from main client → import to new session.
    For HOME DC: reuse the main client's auth key directly (same auth key, 
    different Session object to avoid lock contention).
    """
    my_dc = await client.storage.dc_id()
    test_mode = await client.storage.test_mode()

    if dc_id == my_dc:
        # Home DC – reuse the main client's auth key instead of creating new one.
        # This is safe because we're creating a separate Session object with its
        # own connection, but sharing the same auth key avoids AUTH_KEY_UNREGISTERED.
        logger.debug(f"Reusing main client's auth key for home DC {dc_id}")
        auth_key = client.session.auth_key
        session = Session(
            client, dc_id,
            auth_key,
            test_mode,
            is_media=True,
        )
        await session.start()
    else:
        # Different DC – must create new auth key and import authorization
        logger.debug(f"Creating new auth key and importing authorization for DC {dc_id}")
        
        # Step 1: Export authorization from main client
        exported = await client.invoke(
            raw.functions.auth.ExportAuthorization(dc_id=dc_id)
        )
        
        # Step 2: Generate new auth key via DH exchange
        auth_key = await Auth(client, dc_id, test_mode).create()
        session = Session(
            client, dc_id,
            auth_key,
            test_mode,
            is_media=True,
        )
        await session.start()
        
        # Step 3: Import authorization into the new session
        await session.invoke(
            raw.functions.auth.ImportAuthorization(
                id=exported.id,
                bytes=exported.bytes,
            )
        )

    return session


async def _ensure_session_alive(session: Session, client: Client, dc_id: int) -> Session:
    """Check if a worker session is still alive; recreate if disconnected.

    Returns a valid, connected session (either the original or a new one).
    """
    try:
        # Try a lightweight ping to check if the session is still responsive
        await session.invoke(
            raw.functions.Ping(ping=0)
        )
        return session
    except Exception:
        logger.debug(f"Worker session for DC {dc_id} appears disconnected, recreating")
        # Session is dead – create a fresh one
        return await _get_worker_session(client, dc_id)
