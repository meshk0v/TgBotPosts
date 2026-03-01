import asyncio
import contextlib
import logging
import os
import random
import time
from dataclasses import dataclass
from typing import Any, Optional

from dotenv import load_dotenv
from pyrogram import Client, filters, raw
from pyrogram.errors import RPCError


@dataclass
class Settings:
    api_id: int
    api_hash: str
    session_name: str
    session_dir: str
    source_chats_raw: list[str]
    target_chat_id: str
    target_topic_id: Optional[int]
    copy_mode: bool
    log_file: str
    debug_updates: bool
    poll_interval_seconds: int


def _parse_bool(value: str, default: bool = True) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def load_settings() -> Settings:
    load_dotenv()

    api_id = os.getenv("API_ID")
    api_hash = os.getenv("API_HASH")
    session_name = os.getenv("SESSION_NAME", "user_session")
    session_dir = os.getenv("SESSION_DIR", ".")
    source_chats = os.getenv("SOURCE_CHATS", "")
    target_chat_id = os.getenv("TARGET_CHAT_ID")
    target_topic_id = os.getenv("TARGET_TOPIC_ID", "").strip()
    copy_mode = _parse_bool(os.getenv("COPY_MODE", "true"), default=True)
    log_file = os.getenv("LOG_FILE", "./logs/relay.log")
    debug_updates = _parse_bool(os.getenv("DEBUG_UPDATES", "false"), default=False)
    poll_interval_seconds = int(os.getenv("POLL_INTERVAL_SECONDS", "30"))

    if not api_id:
        raise ValueError("API_ID is required")
    if not api_hash:
        raise ValueError("API_HASH is required")
    if not source_chats:
        raise ValueError("SOURCE_CHATS is required")
    if not target_chat_id:
        raise ValueError("TARGET_CHAT_ID is required")

    source_chats_raw = [item.strip() for item in source_chats.split(",") if item.strip()]
    if not source_chats_raw:
        raise ValueError("SOURCE_CHATS must contain at least one chat")

    topic_id = int(target_topic_id) if target_topic_id else None

    return Settings(
        api_id=int(api_id),
        api_hash=api_hash,
        session_name=session_name,
        session_dir=session_dir,
        source_chats_raw=source_chats_raw,
        target_chat_id=target_chat_id,
        target_topic_id=topic_id,
        copy_mode=copy_mode,
        log_file=log_file,
        debug_updates=debug_updates,
        poll_interval_seconds=poll_interval_seconds,
    )


def configure_logging(log_file: str) -> None:
    log_dir = os.path.dirname(log_file)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    handlers = [
        logging.StreamHandler(),
        logging.FileHandler(log_file, encoding="utf-8"),
    ]

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        handlers=handlers,
    )


def _normalize_chat_id(raw: str):
    try:
        return int(raw)
    except ValueError:
        return raw


async def _find_chat_via_dialogs(client: Client, chat_id: int):
    async for dialog in client.get_dialogs():
        if dialog.chat and dialog.chat.id == chat_id:
            return dialog.chat
    return None


async def _resolve_chat(client: Client, chat_ref: Any, logger: logging.Logger):
    try:
        return await client.get_chat(chat_ref)
    except (RPCError, ValueError) as e:
        if not isinstance(chat_ref, int):
            raise

        chat = await _find_chat_via_dialogs(client, chat_ref)
        if chat is None:
            raise
        logger.info("Chat resolved via dialogs fallback: %s", chat_ref)
        return chat


async def _initialize_chat_cache(
    client: Client,
    source_chats: list[Any],
    target_chat: Any,
    logger: logging.Logger,
) -> tuple[Any, set[int]]:
    # Warm up source chats so peer cache is populated before first updates.
    resolved_source_ids: set[int] = set()
    for source_chat in source_chats:
        try:
            chat = await _resolve_chat(client, source_chat, logger)
            resolved_source_ids.add(chat.id)
            logger.info(
                "Source chat initialized: ref=%s resolved_id=%s type=%s title=%s",
                source_chat,
                chat.id,
                chat.type,
                chat.title,
            )
        except (RPCError, ValueError) as e:
            logger.warning("Source chat init failed: %s (%s)", source_chat, e)

    target = await _resolve_chat(client, target_chat, logger)
    logger.info(
        "Target chat initialized: resolved_id=%s title=%s",
        target.id,
        target.title,
    )
    return await client.resolve_peer(target_chat), resolved_source_ids


async def _copy_to_target(client: Client, source_message, target_chat, topic_id: Optional[int]) -> None:
    if topic_id is None:
        await client.copy_message(
            chat_id=target_chat,
            from_chat_id=source_message.chat.id,
            message_id=source_message.id,
        )
        return

    try:
        # For user sessions, topic routing is typically done by replying to the topic starter message.
        await client.copy_message(
            chat_id=target_chat,
            from_chat_id=source_message.chat.id,
            message_id=source_message.id,
            reply_to_message_id=topic_id,
        )
    except RPCError:
        # Fallback for clients/chats that expect explicit thread id.
        await client.copy_message(
            chat_id=target_chat,
            from_chat_id=source_message.chat.id,
            message_id=source_message.id,
            message_thread_id=topic_id,
        )


async def _copy_batch_to_target(
    client: Client, source_messages: list, target_chat, topic_id: Optional[int]
) -> None:
    for source_message in source_messages:
        await _copy_to_target(client, source_message, target_chat, topic_id)


async def _forward_to_target(
    client: Client, from_chat_id: int, message_ids: list[int], to_peer, topic_id: Optional[int]
) -> None:
    from_peer = await client.resolve_peer(from_chat_id)

    await client.invoke(
        raw.functions.messages.ForwardMessages(
            from_peer=from_peer,
            id=message_ids,
            random_id=[random.randint(1, 2**63 - 1) for _ in message_ids],
            to_peer=to_peer,
            top_msg_id=topic_id,
        )
    )


async def _collect_post_messages(client: Client, message) -> list:
    if message.media_group_id is None:
        return [message]

    group = await client.get_media_group(message.chat.id, message.id)
    return sorted(group, key=lambda m: m.id)


async def main() -> None:
    settings = load_settings()
    configure_logging(settings.log_file)
    logger = logging.getLogger("relay")

    app = Client(
        name=settings.session_name,
        api_id=settings.api_id,
        api_hash=settings.api_hash,
        workdir=settings.session_dir,
    )

    source_chats = [_normalize_chat_id(x) for x in settings.source_chats_raw]
    target_chat = _normalize_chat_id(settings.target_chat_id)
    target_peer = None
    resolved_source_ids: set[int] = set()
    processed_media_groups: dict[str, float] = {}
    last_seen_ids: dict[int, int] = {}

    async def process_source_message(client: Client, message, origin: str) -> None:
        nonlocal target_peer, resolved_source_ids, processed_media_groups, last_seen_ids

        if message.chat is None:
            return

        if resolved_source_ids and message.chat.id not in resolved_source_ids:
            return

        if settings.debug_updates:
            logger.info(
                "Incoming source update (%s): chat_id=%s type=%s msg_id=%s media_group=%s",
                origin,
                message.chat.id,
                message.chat.type,
                message.id,
                message.media_group_id,
            )

        if message.media_group_id is not None:
            media_group_key = f"{message.chat.id}:{message.media_group_id}"
            if media_group_key in processed_media_groups:
                logger.debug("Skipping already processed media group: %s", media_group_key)
                return
            # Give Telegram a brief moment so all album items are available.
            await asyncio.sleep(1.0)

        try:
            source_messages = await _collect_post_messages(client, message)
        except RPCError as e:
            logger.warning(
                "Failed to fetch media group, fallback to single message: chat=%s msg=%s error=%s",
                message.chat.id,
                message.id,
                e,
            )
            source_messages = [message]

        if message.media_group_id is not None:
            media_group_key = f"{message.chat.id}:{message.media_group_id}"
            processed_media_groups[media_group_key] = time.time()
            # Keep memory bounded.
            if len(processed_media_groups) > 1000:
                cutoff = time.time() - 24 * 60 * 60
                processed_media_groups = {
                    key: ts for key, ts in processed_media_groups.items() if ts >= cutoff
                }

        if not source_messages:
            return
        message_ids = [m.id for m in source_messages]
        last_seen_ids[message.chat.id] = max(last_seen_ids.get(message.chat.id, 0), max(message_ids))

        logger.info(
            "New post (%s): source_chat=%s source_msg=%s copy_mode=%s batch_size=%s media_group=%s",
            origin,
            message.chat.id,
            message.id,
            settings.copy_mode,
            len(source_messages),
            message.media_group_id,
        )

        try:
            if settings.copy_mode:
                await _copy_batch_to_target(
                    client, source_messages, target_chat, settings.target_topic_id
                )
            else:
                try:
                    if target_peer is None:
                        target_peer = await client.resolve_peer(target_chat)
                    await _forward_to_target(
                        client,
                        from_chat_id=message.chat.id,
                        message_ids=message_ids,
                        to_peer=target_peer,
                        topic_id=settings.target_topic_id,
                    )
                except (RPCError, ValueError):
                    logger.warning(
                        "Forward failed; fallback to copy for topic support: chat=%s msg=%s",
                        message.chat.id,
                        message.id,
                    )
                    await _copy_batch_to_target(
                        client, source_messages, target_chat, settings.target_topic_id
                    )

            logger.info(
                "Delivered (%s): source_chat=%s source_msg=%s -> target_chat=%s topic=%s",
                origin,
                message.chat.id,
                message.id,
                target_chat,
                settings.target_topic_id,
            )
        except (RPCError, ValueError) as e:
            logger.exception(
                "Failed to relay message (%s): source_chat=%s source_msg=%s error=%s",
                origin,
                message.chat.id,
                message.id,
                e,
            )

    async def poll_sources_loop() -> None:
        while True:
            try:
                for chat_id in sorted(resolved_source_ids):
                    history = [m async for m in app.get_chat_history(chat_id, limit=10)]
                    if not history:
                        continue
                    history = list(reversed(history))
                    baseline = last_seen_ids.get(chat_id, 0)
                    for item in history:
                        if item.id > baseline:
                            await process_source_message(app, item, origin="poll")
                    last_seen_ids[chat_id] = max(last_seen_ids.get(chat_id, 0), history[-1].id)
            except Exception:
                logger.exception("Polling iteration failed")
            await asyncio.sleep(settings.poll_interval_seconds)

    @app.on_message(filters.all)
    async def relay_handler(client: Client, message):
        await process_source_message(client, message, origin="rt")

    logger.info("Watching source chats: %s", settings.source_chats_raw)
    logger.info("Target chat: %s | topic: %s", settings.target_chat_id, settings.target_topic_id)
    logger.info("Session directory: %s | Log file: %s", settings.session_dir, settings.log_file)
    logger.info("Debug updates: %s", settings.debug_updates)
    logger.info("Poll interval (seconds): %s", settings.poll_interval_seconds)

    async with app:
        try:
            target_peer, resolved_source_ids = await _initialize_chat_cache(
                client=app,
                source_chats=source_chats,
                target_chat=target_chat,
                logger=logger,
            )
            logger.info(
                "Chat cache initialized successfully: sources_resolved=%s/%s",
                len(resolved_source_ids),
                len(source_chats),
            )
            logger.info("Resolved source chat ids: %s", sorted(resolved_source_ids))
            for source_id in sorted(resolved_source_ids):
                latest = [m async for m in app.get_chat_history(source_id, limit=1)]
                if latest:
                    last_seen_ids[source_id] = latest[0].id
            logger.info("Polling baselines initialized for %s source chats", len(last_seen_ids))
        except (RPCError, ValueError) as e:
            logger.exception("Target chat init failed: %s", e)
            raise

        poll_task = asyncio.create_task(poll_sources_loop())
        try:
            await asyncio.Event().wait()
        finally:
            poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await poll_task


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
