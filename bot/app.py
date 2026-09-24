"""The application context: one object that owns every long lived service.

It is created once in :mod:`bot.main`, injected into handlers by
:class:`bot.middlewares.context.ContextMiddleware`, and it implements the two
queue workers:

```
user link -> _handle_fetch -> metadata -> plan_route()
                                   |          +--> direct  -> MediaSender (url)
                                   |          +--> process -> _handle_process
                                   |                            |  (SDK download + mux)
                                   |                            +-> MediaSender (upload)
```
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Optional, Sequence

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from downloader.core.models import DownloadedFile, PostMetadata

from bot.config import Settings
from bot.db import repo
from bot.db.base import db
from bot.services.broadcast import BroadcastService
from bot.services.downloader_service import DownloaderService
from bot.services.platforms import PlatformRegistry, PlatformSnapshot
from bot.services.queues import FetchJob, PlatformQueueManager, ProcessJob
from bot.services.routing import Action, RoutePlan, choose_quality, plan_route
from bot.services.sender import Delivery, MediaSender
from bot.services.status import StatusReporter
from bot.ui.descriptions import build_caption, build_status
from bot.utils.text import escape, format_size

logger = logging.getLogger(__name__)

PHOTO_UPLOAD_LIMIT = 10 * 1024 * 1024
"""Telegram refuses a `photo` larger than this; send it as a document instead."""


class AppContext:
    """Everything the bot needs, plus the two queue workers."""

    def __init__(self, *, bot: Bot, settings: Settings) -> None:
        self.bot = bot
        self.settings = settings
        self.me = None
        self.bot_username = ""
        self.started_at = time.time()

        self.downloader = DownloaderService(
            proxy=settings.proxy,
            cookies_file=settings.cookies_file,
            instagram_session_file=settings.instagram_session_file,
            cache_dir=settings.temp_dir / "cache",
            timeout=settings.request_timeout,
        )
        self.registry = PlatformRegistry()
        self.status = StatusReporter(bot)
        self.sender = MediaSender(
            bot=bot,
            downloader=self.downloader,
            upload_limit=settings.telegram_upload_limit,
            album_chunk_size=settings.album_chunk_size,
        )
        self.broadcast = BroadcastService(bot, rate_per_second=settings.broadcast_rate_per_second)
        self.queues = PlatformQueueManager(
            fetch_worker=self._handle_fetch,
            process_worker=self._handle_process,
            configs=self.registry.queue_configs(),
            processing_queue_size=settings.processing_queue_size,
            processing_concurrency=settings.processing_concurrency,
            processing_max_ram_bytes=settings.processing_max_ram_bytes,
            platform_limits=self.registry.processing_limits(),
        )

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self.settings.ensure_directories()
        self.me = await self.bot.get_me()
        self.bot_username = self.me.username or ""
        await self.downloader.start()
        await self.registry.refresh()
        self.apply_settings()
        await self.queues.start()
        logger.info(
            "context ready as @%s with platforms %s", self.bot_username, list(self.registry.enabled)
        )

    async def stop(self) -> None:
        await self.queues.stop()
        await self.downloader.close()

    def apply_settings(self) -> None:
        """Push the current registry/queue settings into the live queues."""
        for name, config in self.registry.queue_configs().items():
            queue = self.queues.platforms.get(name)
            if queue is not None:
                queue.apply_config(config)
        self.queues.processing.apply_config(
            queue_size=self.settings.processing_queue_size,
            concurrency=self.settings.processing_concurrency,
            max_ram_bytes=self.settings.processing_max_ram_bytes,
            platform_limits=self.registry.processing_limits(),
        )

    # -------------------------------------------------------------- helpers
    @property
    def footer(self) -> Optional[str]:
        return f"@{self.bot_username}" if self.bot_username else None

    def route_for(self, meta: PostMetadata, platform: str, quality: str) -> RoutePlan:
        setting = self.registry.get(platform)
        return plan_route(
            meta,
            quality=quality,
            max_item_bytes=setting.max_media_size_bytes,
            photo_url_limit=self.settings.telegram_url_photo_limit,
            video_url_limit=setting.direct_url_video_limit,
            upload_limit=self.settings.telegram_upload_limit,
            photo_upload_limit=PHOTO_UPLOAD_LIMIT,
        )

    def caption_for(
        self,
        meta: PostMetadata,
        *,
        quality: Optional[str],
        size_bytes: Optional[int] = None,
        note: Optional[str] = None,
    ) -> str:
        return build_caption(
            meta, quality=quality, size_bytes=size_bytes, footer=self.footer, note=note
        )

    def original_post_markup(self, meta: PostMetadata) -> Optional[InlineKeyboardMarkup]:
        url = meta.requested_url or meta.url or meta.permalink or meta.external_url
        if not url:
            return None
        return InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Original post", url=url)]]
        )

    async def reply(
        self,
        chat_id: int,
        text: str,
        *,
        message_id: Optional[int] = None,
        thread_id: Optional[int] = None,
    ) -> None:
        kwargs: dict = {}
        if message_id is not None:
            from aiogram.types import ReplyParameters

            kwargs["reply_parameters"] = ReplyParameters(
                message_id=message_id, allow_sending_without_reply=True
            )
        if thread_id is not None:
            kwargs["message_thread_id"] = thread_id
        try:
            await self.bot.send_message(chat_id, text, **kwargs)
        except TelegramForbiddenError:
            logger.info("cannot message chat %s", chat_id)
        except Exception as exc:  # noqa: BLE001 - never let a notice break a job
            logger.warning("reply to %s failed: %s", chat_id, exc)

    async def _record(self, **values: Any) -> None:
        try:
            async with db.session() as session:
                await repo.record_usage(session, **values)
        except Exception:  # noqa: BLE001 - analytics must never break a request
            logger.exception("could not record usage")

    # ----------------------------------------------------------- fetch lane
    async def _handle_fetch(self, job: FetchJob) -> None:
        started = time.perf_counter()
        platform = job.platform
        setting = self.registry.get(platform)
        await self.status.edit(
            job.chat_id,
            job.status_message_id,
            build_status(platform, "fetching", queue_depth=self.queues.depth_of(platform)),
            force=True,
        )

        try:
            meta = await self.downloader.get_metadata(job.url)
        except Exception as exc:  # noqa: BLE001 - any extraction failure is a user message
            await self._fail_fetch(job, exc, started)
            return

        requested = job.quality or setting.quality
        quality, downgrade = choose_quality(
            meta,
            requested=requested,
            upload_limit=self.settings.telegram_upload_limit,
            max_item_bytes=setting.max_media_size_bytes,
        )
        route = self.route_for(meta, platform, quality)
        size_total = route.direct_bytes + route.process_bytes
        if downgrade:
            logger.info("%s: %s", job.url, downgrade)

        if not route.planned:
            reason = route.rejected[0].reason if route.rejected else "no downloadable media was found"
            await self.status.drop(job.chat_id, job.status_message_id)
            await self.reply(
                job.chat_id,
                f"\u2717 {escape(meta.title or 'This post')}\n{escape(reason)}",
                message_id=job.message_id,
                thread_id=job.thread_id,
            )
            await self._record(
                platform=platform,
                outcome="rejected",
                user_tg_id=job.user_tg_id,
                chat_tg_id=job.chat_id,
                user_id=job.user_db_id,
                chat_id=job.chat_db_id,
                url=job.url,
                media_type=str(meta.media_type),
                media_group_type=str(meta.media_group_type),
                quality=quality,
                item_count=len(meta.items),
                detail=reason,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
            return

        caption = self.caption_for(
            meta, quality=quality, size_bytes=size_total or None, note=downgrade
        )
        original_markup = self.original_post_markup(meta)
        delivery = Delivery()
        if route.direct:
            try:
                delivery.merge(
                    await self.sender.send_plans(
                        job.chat_id,
                        route.direct,
                        caption=caption,
                        reply_to=job.message_id,
                        thread_id=job.thread_id,
                        reply_markup=original_markup,
                    )
                )
            except TelegramForbiddenError:
                await self._forbidden(job)
                return
            caption = None

        queued = False
        queued_reason = ""
        if route.process:
            ram = route.process_ram_estimate(
                factor=self.settings.processing_ram_factor,
                fallback=self.settings.processing_default_estimate_bytes,
            )
            queued = ProcessJob(
                platform=platform,
                url=job.url,
                meta=meta,
                route=route,
                quality=quality,
                chat_id=job.chat_id,
                message_id=job.message_id,
                thread_id=job.thread_id,
                user_tg_id=job.user_tg_id,
                user_db_id=job.user_db_id,
                chat_db_id=job.chat_db_id,
                status_message_id=job.status_message_id,
                reserved_ram=ram,
            )
            accepted, queued_reason = self.queues.submit_process(queued)
            if accepted:
                queued = True
                await self.status.edit(
                    job.chat_id,
                    job.status_message_id,
                    build_status(
                        platform,
                        "queued",
                        detail=f"{len(route.process)} item(s) · {format_size(route.process_bytes)}",
                    ),
                    force=True,
                )
                job.status_message_id = None  # the processing worker owns it now
            else:
                await self.reply(
                    job.chat_id,
                    f"\u25f7 {escape(queued_reason or 'busy')}\nTry again in a moment.",
                    message_id=job.message_id,
                    thread_id=job.thread_id,
                )

        if route.rejected:
            await self.reply(
                job.chat_id,
                self._rejection_text(route),
                message_id=job.message_id,
                thread_id=job.thread_id,
            )

        status_id = job.status_message_id
        if not queued:
            if delivery.errors:
                await self.status.edit(
                    job.chat_id,
                    status_id,
                    build_status(platform, "error", detail=delivery.errors[0]),
                    force=True,
                )
                if status_id is None:
                    await self.reply(
                        job.chat_id,
                        "\u2717 Some items failed:\n"
                        + "\n".join(escape(error) for error in delivery.errors[:5]),
                        message_id=job.message_id,
                        thread_id=job.thread_id,
                    )
            else:
                await self.status.drop(job.chat_id, status_id)

        # The fetch stage never counts a hand-off as a send: a queued post is
        # finished (and recorded) by the processing worker, so recording "ok"
        # here too would count every processed post twice.
        if queued:
            outcome = "queued"
        elif route.process:
            outcome = "error"
        elif delivery.errors:
            outcome = "partial"
        else:
            outcome = "direct"
        await self._record(
            platform=platform,
            outcome=outcome,
            user_tg_id=job.user_tg_id,
            chat_tg_id=job.chat_id,
            user_id=job.user_db_id,
            chat_id=job.chat_db_id,
            url=job.url,
            media_type=str(meta.media_type),
            media_group_type=str(meta.media_group_type),
            quality=quality,
            item_count=len(meta.items),
            sent_count=delivery.sent,
            bytes_total=delivery.bytes_sent,
            detail=queued_reason or None,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
        await self._bump(job)

    async def _fail_fetch(self, job: FetchJob, exc: BaseException, started: float) -> None:
        message = " ".join(str(exc).split())[:180] or type(exc).__name__
        await self.status.drop(job.chat_id, job.status_message_id)
        await self.reply(
            job.chat_id,
            f"\u2717 Could not read that link\n{escape(message)}",
            message_id=job.message_id,
            thread_id=job.thread_id,
        )
        await self._record(
            platform=job.platform,
            outcome="error",
            user_tg_id=job.user_tg_id,
            chat_tg_id=job.chat_id,
            user_id=job.user_db_id,
            chat_id=job.chat_db_id,
            url=job.url,
            detail=message,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
        await self._bump(job, failed=True)

    # ------------------------------------------------------- processing lane
    async def _handle_process(self, job: ProcessJob) -> None:
        started = time.perf_counter()
        platform = job.platform
        await self.status.edit(
            job.chat_id,
            job.status_message_id,
            build_status(
                platform,
                "processing",
                detail=f"{len(job.route.process)} item(s) · {format_size(job.route.process_bytes)}",
            ),
            force=True,
        )

        mux_items = [item for item in job.route.process if item.action == Action.MUX]
        server_items = [item for item in job.route.process if item.action != Action.MUX]
        caption = self.caption_for(job.meta, quality=job.quality, size_bytes=job.route.process_bytes)
        original_markup = self.original_post_markup(job.meta)
        delivery = Delivery()
        files: list[DownloadedFile] = []
        notes: list[str] = []

        if mux_items:
            try:
                result = await self.downloader.download(
                    job.meta,
                    quality=job.quality,
                    only=[item.index for item in mux_items],
                    max_size_bytes=self.settings.telegram_upload_limit,
                )
                files = list(result.files)
                notes.extend(result.errors.values())
                notes.extend(result.warnings)
            except Exception as exc:  # noqa: BLE001 - surface the reason to the user
                notes.append(" ".join(str(exc).split())[:180])

        try:
            if files:
                delivery.merge(
                    await self.sender.send_files(
                        job.chat_id,
                        files,
                        caption=caption,
                        reply_to=job.message_id,
                        thread_id=job.thread_id,
                        reply_markup=original_markup,
                    )
                )
                caption = None
            if server_items:
                delivery.merge(
                    await self.sender.send_plans(
                        job.chat_id,
                        server_items,
                        caption=caption,
                        reply_to=job.message_id,
                        thread_id=job.thread_id,
                        reply_markup=original_markup,
                    )
                )
        except TelegramForbiddenError:
            await self._forbidden(job)
            self._cleanup(files)
            return

        self._cleanup(files)
        delivery.notes.extend(notes)

        if delivery.sent:
            await self.status.drop(job.chat_id, job.status_message_id)
        else:
            errors = delivery.errors or notes or ["nothing was delivered"]
            await self.status.edit(
                job.chat_id,
                job.status_message_id,
                build_status(platform, "error", detail="; ".join(errors[:2])),
                force=True,
            )

        await self._record(
            platform=platform,
            outcome="ok" if delivery.sent and not delivery.errors else ("partial" if delivery.sent else "error"),
            user_tg_id=job.user_tg_id,
            chat_tg_id=job.chat_id,
            user_id=job.user_db_id,
            chat_id=job.chat_db_id,
            url=job.url,
            media_type=str(job.meta.media_type),
            media_group_type=str(job.meta.media_group_type),
            quality=job.quality,
            item_count=len(job.route.process),
            sent_count=delivery.sent,
            bytes_total=delivery.bytes_sent,
            detail="; ".join((delivery.errors + notes)[:2]) or None,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )

    # -------------------------------------------------------------- utility
    def _rejection_text(self, route: RoutePlan) -> str:
        lines = ["\u2717 Some media was skipped:"]
        for rejected in route.rejected[:5]:
            lines.append(f"\u25b8 item #{rejected.index + 1} \u00b7 {escape(rejected.reason)}")
        return "\n".join(lines)

    def _cleanup(self, files: Sequence[DownloadedFile]) -> None:
        temp_root = self.settings.temp_dir.resolve()
        for file in files:
            path = file.path
            if path is None:
                continue
            try:
                resolved = Path(path).resolve()
            except OSError:  # pragma: no cover - defensive
                continue
            if temp_root in resolved.parents and resolved.exists():
                try:
                    resolved.unlink()
                except OSError:  # pragma: no cover - defensive
                    logger.debug("could not remove %s", resolved)

    async def _forbidden(self, job: Any) -> None:
        logger.info("bot was blocked or removed from chat %s", job.chat_id)
        try:
            async with db.session() as session:
                if job.chat_tg_id and job.chat_tg_id != job.chat_id:
                    await repo.set_chat_banned(session, job.chat_tg_id, True, "bot has no access")
        except Exception:  # noqa: BLE001 - best effort
            pass

    async def _bump(self, job: FetchJob, *, failed: bool = False) -> None:
        try:
            async with db.session() as session:
                await repo.bump_user_requests(session, job.user_tg_id, failed=failed)
                if job.thread_id is not None or job.chat_db_id is not None:
                    await repo.bump_chat_requests(session, job.chat_id)
        except Exception:  # noqa: BLE001 - counters are not critical
            logger.debug("could not bump counters for %s", job.user_tg_id)


__all__ = ["AppContext", "PHOTO_UPLOAD_LIMIT"]
