"""Telegram media download worker.

Supports parallel chunk downloading for large files and sends
completion / failure notifications back to the originating TG chat.
"""

import asyncio
import os
import shutil
import time
from pathlib import Path

from loguru import logger
from pyrogram import raw
from pyrogram.types import Message
from pyrogram.utils import get_channel_id
from sqlalchemy import update

from app.core.database import async_session_factory
from app.core.tg_client import get_worker_client
from app.models.task import SourceType, Task, TaskStatus
from app.services.notifier import notify_complete, notify_failed
from app.services.tg_downloader import download_tg_file
from app.core.settings import settings
from app.workers.retry_handler import schedule_retry

# Per-task throttle state for progress callbacks (task_id -> timestamp/start)
_progress_ts: dict[int, float] = {}
_progress_start: dict[int, float] = {}


def _is_file_reference_expired(exc: BaseException) -> bool:
    return "file_reference_expired" in str(exc).lower()


def _build_peer_from_redis(chat_id: int):
    """Build a raw InputPeer from Redis-cached metadata (mirror of notifier.py)."""
    try:
        from app.core.redis import redis_conn
        raw_hash = redis_conn.get(f"tg:peer_hash:{chat_id}")
        peer_type = redis_conn.get(f"tg:peer_type:{chat_id}")
        pt = peer_type.decode() if isinstance(peer_type, bytes) else (peer_type or "")
        if "Chat" in pt and "Channel" not in pt:
            return raw.types.InputPeerChat(chat_id=abs(int(chat_id)))
        if not raw_hash:
            return None
        access_hash = int(raw_hash)
        if "Channel" in pt:
            return raw.types.InputPeerChannel(
                channel_id=get_channel_id(int(chat_id)),
                access_hash=access_hash,
            )
        return raw.types.InputPeerUser(
            user_id=abs(int(chat_id)),
            access_hash=access_hash,
        )
    except Exception:
        return None


async def _warm_peer_cache(client, chat_id: int) -> None:
    """Pre-populate the in-memory Pyrogram client's peer cache.

    Without this, worker in-memory sessions cannot resolve channel/user
    IDs, causing ``PEER_ID_INVALID`` on every ``get_messages()`` /
    ``get_chat()`` call.

    Three-tier strategy:
      1. Redis-cached peer → raw API injection
      2. ``client.resolve_peer()`` → dynamic resolution from Telegram
      3. ignore silently (downstream will handle failure)
    """
    # Tier 1: Redis cache
    peer = _build_peer_from_redis(chat_id)
    if peer is not None:
        try:
            if isinstance(peer, raw.types.InputPeerChannel):
                inp = raw.types.InputChannel(
                    channel_id=peer.channel_id,
                    access_hash=peer.access_hash,
                )
                await client.invoke(raw.functions.channels.GetChannels(id=[inp]))
                return
            elif isinstance(peer, raw.types.InputPeerUser):
                inp = raw.types.InputUser(
                    user_id=peer.user_id,
                    access_hash=peer.access_hash,
                )
                await client.invoke(raw.functions.users.GetUsers(id=[inp]))
                return
        except Exception as exc:
            logger.debug(f"_warm_peer_cache Redis tier failed for chat {chat_id}: {exc}")

    # Tier 2: dynamic resolution via Pyrogram (slower but always works)
    try:
        await client.resolve_peer(chat_id)
        return
    except Exception as exc:
        logger.debug(f"_warm_peer_cache resolve_peer failed for chat {chat_id}: {exc}")


async def _load_origin_message(client, chat_id: int, message_id: int) -> Message:
    """Fetch the original message so we can obtain a fresh media file_id.

    In-memory worker sessions lack the SQLite peer cache that the main
    listener has.  We first warm the peer cache from Redis, then use
    Pyrogram's ``get_messages()``.  If that fails we fall back to the
    raw API using the cached peer directly.
    """
    await _warm_peer_cache(client, chat_id)

    # Try Pyrogram first (most reliable, returns Message objects)
    try:
        msg = await client.get_messages(chat_id, message_id)
        if msg:
            return msg
    except Exception:
        pass

    # Fallback: raw API using Redis-cached peer
    peer = _build_peer_from_redis(chat_id)
    if peer is not None:
        # Channel fallback
        if isinstance(peer, (raw.types.InputPeerChannel, raw.types.InputPeerChat)):
            try:
                result = await client.invoke(
                    raw.functions.channels.GetMessages(
                        channel=peer,
                        id=[raw.types.InputMessageID(id=message_id)],
                    )
                )
                msgs = result.messages if hasattr(result, 'messages') else []
                if msgs:
                    return await Message._parse(client, msgs[0])
            except Exception:
                pass
        # User fallback
        else:
            try:
                result = await client.invoke(
                    raw.functions.messages.GetMessages(
                        id=[raw.types.InputMessageID(id=message_id)],
                    )
                )
                msgs = result.messages if hasattr(result, 'messages') else []
                if msgs:
                    return await Message._parse(client, msgs[0])
            except Exception:
                pass
    raise RuntimeError(
        f"Could not fetch origin message {message_id} from chat {chat_id}"
    )


def _extract_media_from_message(message: Message, source_type: SourceType) -> tuple[str, str, int]:
    if source_type == SourceType.TG_VIDEO and message.video:
        media = message.video
        return media.file_id, (media.file_name or f"video_{message.id}.mp4"), (media.file_size or 0)
    if source_type == SourceType.TG_DOCUMENT and message.document:
        media = message.document
        return media.file_id, (media.file_name or f"doc_{message.id}"), (media.file_size or 0)
    if source_type == SourceType.TG_PHOTO and message.photo:
        media = message.photo
        return media.file_id, f"photo_{message.id}.jpg", (media.file_size or 0)
    if source_type == SourceType.TG_AUDIO and message.audio:
        media = message.audio
        return media.file_id, (media.file_name or f"audio_{message.id}.mp3"), (media.file_size or 0)
    raise RuntimeError(
        f"Origin message {message.id} no longer contains expected media for {source_type.value}"
    )


async def _refresh_media_reference(
    client,
    task_id: int,
    source_type: SourceType,
    chat_id: int,
    message_id: int,
) -> tuple[str, str, int]:
    """Refresh expired TG file references from the original message."""
    message = await _load_origin_message(client, chat_id, message_id)
    fresh_file_id, fresh_file_name, fresh_file_size = _extract_media_from_message(message, source_type)

    # Validate that the refresh actually succeeded
    if not fresh_file_id:
        raise RuntimeError(
            f"Failed to refresh media reference - message {message_id} may be deleted or inaccessible"
        )
    if fresh_file_size <= 0:
        logger.warning(
            f"Task #{task_id}: refreshed file reference has size=0, "
            f"media may be unavailable"
        )

    async with async_session_factory() as session:
        task = await session.get(Task, task_id)
        if task:
            task.telegram_file_id = fresh_file_id
            task.file_name = fresh_file_name
            if fresh_file_size > 0:
                task.file_size = fresh_file_size
            await session.commit()

    logger.info(
        f"Task #{task_id}: refreshed expired file reference from origin message {message_id}"
    )
    return fresh_file_id, fresh_file_name, fresh_file_size


def _cleanup_task_files(task_id: int, *file_paths: Path) -> None:
    """Clean up all temporary files for a failed task.

    Also scans temp directory for any leftover files with the task_id prefix.
    """
    for path in file_paths:
        if path:
            path.unlink(missing_ok=True)
            # Also clean up common temp suffixes
            Path(str(path) + ".temp").unlink(missing_ok=True)
            Path(str(path) + ".download").unlink(missing_ok=True)


async def _do_download(task_id: int):
    """Actual async download logic for TG media."""
    # ---- load task --------------------------------------------------
    async with async_session_factory() as session:
        task = await session.get(Task, task_id)
        if not task:
            logger.error(f"Task #{task_id} not found")
            return

        if task.status not in (TaskStatus.PENDING, TaskStatus.RETRYING):
            logger.warning(f"Task #{task_id} skipped, status={task.status}")
            return

        task.status = TaskStatus.DOWNLOADING
        await session.commit()

        # Copy fields we need outside session scope
        file_id = task.telegram_file_id
        file_name = task.file_name
        file_size = task.file_size or 0
        source_type = task.source_type
        chat_id = task.telegram_chat_id
        message_id = task.telegram_message_id

    logger.info(f"Starting TG download for task #{task_id}: {file_name}")

    # ---- paths ------------------------------------------------------
    type_dir_map = {
        SourceType.TG_VIDEO: "telegram/video",
        SourceType.TG_DOCUMENT: "telegram/document",
        SourceType.TG_PHOTO: "telegram/photo",
        SourceType.TG_AUDIO: "telegram/audio",
    }
    sub_dir = type_dir_map.get(SourceType(source_type))
    if sub_dir is None:
        raise ValueError(f"Unsupported source type: {source_type}")
    target_dir = settings.storage_path / sub_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    temp_dir = settings.temp_path
    temp_dir.mkdir(parents=True, exist_ok=True)

    temp_file = temp_dir / f"{file_name}.tmp"
    final_file = target_dir / file_name

    # Defaults for the exception handler — if the DB session fails early
    # these still have safe values instead of raising NameError.
    client = None

    # ---- download ---------------------------------------------------
    try:
        client = await get_worker_client()
        if client is None:
            raise RuntimeError("No Pyrogram client available – session missing or not configured")

        start_time = time.time()
        _progress_start[task_id] = start_time
        _progress_ts[task_id] = 0.0
        refreshed_reference = False

        # Historical tasks may have stale metadata (file_size=0 / expired file_id).
        # Refresh once up front so single-stream downloads don't silently create
        # an empty temp file before we ever see an exception.
        if file_size <= 0 and chat_id and message_id:
            file_id, file_name, refreshed_size = await _refresh_media_reference(
                client=client,
                task_id=task_id,
                source_type=SourceType(source_type),
                chat_id=chat_id,
                message_id=message_id,
            )
            final_file = target_dir / file_name
            temp_file = temp_dir / f"{file_name}.tmp"
            if refreshed_size > 0:
                file_size = refreshed_size
            refreshed_reference = True

        # Use parallel downloader (auto-fallback for small files)
        try:
            await download_tg_file(
                client=client,
                file_id_str=file_id,
                file_size=file_size,
                dest_path=temp_file,
                progress=lambda cur, tot: _progress_callback(
                    task_id, cur, tot),
            )
        except Exception as exc:
            if not _is_file_reference_expired(exc):
                raise

            logger.warning(
                f"Task #{task_id}: file reference expired, refreshing from origin message and retrying once"
            )
            temp_file.unlink(missing_ok=True)
            Path(str(temp_file) + ".temp").unlink(missing_ok=True)

            file_id, file_name, refreshed_size = await _refresh_media_reference(
                client=client,
                task_id=task_id,
                source_type=SourceType(source_type),
                chat_id=chat_id,
                message_id=message_id,
            )
            final_file = target_dir / file_name
            temp_file = temp_dir / f"{file_name}.tmp"
            if refreshed_size > 0:
                file_size = refreshed_size
            refreshed_reference = True

            await download_tg_file(
                client=client,
                file_id_str=file_id,
                file_size=file_size,
                dest_path=temp_file,
                progress=lambda cur, tot: _progress_callback(
                    task_id, cur, tot),
            )

        # Pyrogram single-stream can sometimes swallow FILE_REFERENCE_EXPIRED,
        # log a traceback, and leave behind a 0-byte temp file. Treat that as
        # a stale reference and do one explicit refresh + retry here.
        if (
            temp_file.exists()
            and temp_file.stat().st_size <= 0
            and not refreshed_reference
            and chat_id
            and message_id
        ):
            logger.warning(
                f"Task #{task_id}: temp file is empty after download attempt, refreshing origin message and retrying once"
            )
            temp_file.unlink(missing_ok=True)
            Path(str(temp_file) + ".temp").unlink(missing_ok=True)

            file_id, file_name, refreshed_size = await _refresh_media_reference(
                client=client,
                task_id=task_id,
                source_type=SourceType(source_type),
                chat_id=chat_id,
                message_id=message_id,
            )
            final_file = target_dir / file_name
            temp_file = temp_dir / f"{file_name}.tmp"
            if refreshed_size > 0:
                file_size = refreshed_size

            await download_tg_file(
                client=client,
                file_id_str=file_id,
                file_size=file_size,
                dest_path=temp_file,
                progress=lambda cur, tot: _progress_callback(
                    task_id, cur, tot),
            )

        elapsed = time.time() - start_time
        _progress_ts.pop(task_id, None)
        _progress_start.pop(task_id, None)

        # Move temp -> final
        if temp_file.exists():
            shutil.move(str(temp_file), str(final_file))
        else:
            raise RuntimeError("Downloaded temp file not found")

        actual_size = os.path.getsize(str(final_file))
        if actual_size <= 0:
            raise RuntimeError("Downloaded file is empty (0 bytes)")
        if file_size > 0 and actual_size != file_size:
            raise RuntimeError(
                f"Downloaded file size mismatch: expected {file_size}, got {actual_size}"
            )
        speed = actual_size / elapsed if elapsed > 0 else 0

        # ---- mark completed -----------------------------------------
        async with async_session_factory() as session:
            await session.execute(
                update(Task)
                .where(Task.id == task_id)
                .values(
                    status=TaskStatus.COMPLETED,
                    local_path=str(final_file),
                    file_size=actual_size,
                    downloaded_size=actual_size,
                    speed=speed,
                )
            )
            await session.commit()

        logger.info(
            f"Task #{task_id} completed: {final_file} "
            f"({actual_size} bytes, {speed:.0f} B/s)"
        )

        # ---- notify TG chat (outside DB session) --------------------
        # Preserve variables before exiting the outer try block scope
        notify_chat_id = chat_id
        notify_message_id = message_id
        notify_file_name = file_name
        notify_file_size = actual_size
        notify_speed = speed
        notify_local_path = str(final_file)

        try:
            await notify_complete(
                chat_id=notify_chat_id,
                message_id=notify_message_id,
                file_name=notify_file_name,
                file_size=notify_file_size,
                speed=notify_speed,
                local_path=notify_local_path,
            )
        except Exception as exc:
            logger.warning(f"Task #{task_id}: failed to send completion notification: {exc}")

    except Exception as e:
        logger.error(f"Task #{task_id} failed: {e}")

        _progress_ts.pop(task_id, None)
        _progress_start.pop(task_id, None)

        # Clean up all temporary files
        _cleanup_task_files(task_id, temp_file, final_file)

        # Schedule retry & persist error
        async with async_session_factory() as session:
            task = await session.get(Task, task_id)
            if task:
                task.error_message = str(e)
                schedule_retry(session, task)
                await session.commit()

                # Copy values for notification outside session
                fail_chat_id = chat_id
                fail_message_id = message_id
                fail_file_name = file_name
                fail_retry_count = task.retry_count
                fail_max_retries = task.max_retries

                try:
                    await notify_failed(
                        chat_id=fail_chat_id,
                        message_id=fail_message_id,
                        file_name=fail_file_name,
                        error=str(e),
                        retry_count=fail_retry_count,
                        max_retries=fail_max_retries,
                    )
                except Exception as notify_exc:
                    logger.warning(f"Task #{task_id}: failed to send failure notification: {notify_exc}")


def _progress_callback(task_id: int, current: int, total: int):
    """Write download progress to DB (throttled to at most once per 3 s)."""
    now = time.time()
    last = _progress_ts.get(task_id, 0.0)
    if now - last < 3.0:
        return
    _progress_ts[task_id] = now

    elapsed = now - _progress_start.get(task_id, now)
    speed = current / elapsed if elapsed > 0 else 0.0

    async def _write():
        async with async_session_factory() as session:
            await session.execute(
                update(Task)
                .where(Task.id == task_id)
                .values(downloaded_size=current, speed=speed)
            )
            await session.commit()

    # Schedule the coroutine on the running event loop (we are inside it).
    try:
        loop = asyncio.get_event_loop()
        loop.create_task(_write())
    except Exception as exc:
        logger.warning(f"Task #{task_id}: failed to schedule progress update: {exc}")


def download_tg_media(task_id: int):
    """Entry point for RQ worker (sync wrapper for async logic).

    Uses the persistent worker event loop (see ``app.core.tg_client``)
    so that the Pyrogram client stays alive across all tasks in this
    worker process.  No per-task loop creation — avoids
    ``AUTH_KEY_DUPLICATED`` caused by orphaned Clients on closed loops.
    """
    from app.core.tg_client import run_async
    run_async(_do_download(task_id))
