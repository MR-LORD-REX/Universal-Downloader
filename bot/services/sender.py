"""Turning a plan into Telegram messages.

Three delivery styles, one API:

* **by url** - the CDN link is handed to the Bot API and Telegram fetches the
  file itself. This is the cheap path the project brief asks for: images and
  ready-to-play videos never touch our disk.
* **by server fetch** - we download the bytes and upload them. Used when the
  file is too big for url delivery, when Telegram's fetch fails, or for audio.
* **by muxed file** - the processing queue already produced a local file.

Any url delivery that Telegram refuses is transparently retried through the
server path, so delivery only fails when both lanes fail.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional, Sequence

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import (
    BufferedInputFile,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaAnimation,
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
    ReplyParameters,
)

from downloader.core.models import DownloadedFile
from downloader.core.urls import filename_of

from bot.services.downloader_service import DownloaderService
from bot.services.routing import Action, PlannedItem, SendAs

logger = logging.getLogger(__name__)

_URL_FAILURES = (
    "failed to get http url content",
    "webpage_curl_failed",
    "wrong file identifier",
    "http url specified",
    "failed to get http",
    "wrong remote file identifier",
)

_ALBUM_KINDS = (SendAs.PHOTO, SendAs.VIDEO, SendAs.ANIMATION)

_MEDIA_CLASSES = {
    SendAs.PHOTO: InputMediaPhoto,
    SendAs.VIDEO: InputMediaVideo,
    SendAs.ANIMATION: InputMediaAnimation,
    SendAs.AUDIO: InputMediaAudio,
    SendAs.DOCUMENT: InputMediaDocument,
}


@dataclass(slots=True)
class Delivery:
    """What actually made it to Telegram."""

    sent: int = 0
    bytes_sent: int = 0
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.sent > 0

    def merge(self, other: "Delivery") -> "Delivery":
        self.sent += other.sent
        self.bytes_sent += other.bytes_sent
        self.errors.extend(other.errors)
        self.notes.extend(other.notes)
        return self


def is_url_failure(exc: BaseException) -> bool:
    """True when Telegram refused to fetch a url we handed it."""
    message = str(exc).lower()
    return any(token in message for token in _URL_FAILURES)


class MediaSender:
    """Sends planned items and downloaded files to a chat."""

    def __init__(
        self,
        *,
        bot: Bot,
        downloader: DownloaderService,
        upload_limit: int,
        album_chunk_size: int = 10,
    ) -> None:
        self._bot = bot
        self._downloader = downloader
        self.upload_limit = upload_limit
        self.album_chunk_size = max(2, min(10, album_chunk_size))

    # --------------------------------------------------------------- helpers
    def _reply(self, reply_to: Optional[int], thread_id: Optional[int]) -> dict:
        kwargs: dict = {}
        if reply_to is not None:
            kwargs["reply_parameters"] = ReplyParameters(
                message_id=reply_to, allow_sending_without_reply=True
            )
        if thread_id is not None:
            kwargs["message_thread_id"] = thread_id
        return kwargs

    async def _guard(self, call, *args, **kwargs):
        """Run a Bot API call, absorbing a one-off flood wait."""
        try:
            return await call(*args, **kwargs)
        except TelegramRetryAfter as exc:
            wait = min(getattr(exc, "retry_after", 3) or 3, 30)
            logger.warning("flood wait %ss", wait)
            await asyncio.sleep(wait + 0.5)
            return await call(*args, **kwargs)

    # ----------------------------------------------------------------- entry
    async def send_plans(
        self,
        chat_id: int,
        plans: Sequence[PlannedItem],
        *,
        caption: Optional[str] = None,
        reply_to: Optional[int] = None,
        thread_id: Optional[int] = None,
        reply_markup: Optional[InlineKeyboardMarkup] = None,
    ) -> Delivery:
        """Deliver every planned item, grouping album-capable ones together."""
        delivery = Delivery()
        url_album: list[PlannedItem] = []
        singles: list[PlannedItem] = []

        for plan in plans:
            if plan.action == Action.URL and plan.send_as in _ALBUM_KINDS:
                url_album.append(plan)
            else:
                singles.append(plan)

        if len(url_album) > 1:
            for chunk in _chunks(url_album, self.album_chunk_size):
                delivery.merge(
                    await self._send_album(
                        chat_id,
                        chunk,
                        caption=caption,
                        reply_to=reply_to,
                        thread_id=thread_id,
                        parse_mode="HTML",
                    )
                )
                caption = None
        else:
            singles = url_album + singles

        for plan in singles:
            delivery.merge(
                await self._send_single(
                    chat_id,
                    plan,
                    caption=caption,
                    reply_to=reply_to,
                    thread_id=thread_id,
                    reply_markup=reply_markup if plan is singles[0] else None,
                )
            )
            caption = None
        return delivery

    # ----------------------------------------------------------- single item
    async def _send_single(
        self,
        chat_id: int,
        plan: PlannedItem,
        *,
        caption: Optional[str],
        reply_to: Optional[int],
        thread_id: Optional[int],
        reply_markup: Optional[InlineKeyboardMarkup] = None,
    ) -> Delivery:
        delivery = Delivery()
        kwargs = self._reply(reply_to, thread_id)

        if plan.action == Action.URL and plan.url:
            try:
                await self._guard(
                    self._send_by_url,
                    chat_id,
                    plan,
                    caption=caption,
                    reply_markup=reply_markup,
                    parse_mode="HTML",
                    **kwargs,
                )
                delivery.sent += 1
                delivery.bytes_sent += plan.size or 0
                return delivery
            except TelegramForbiddenError:
                raise
            except (TelegramBadRequest, TelegramAPIError) as exc:
                if not is_url_failure(exc):
                    delivery.errors.append(f"item #{plan.index + 1}: {exc}")
                    return delivery
                delivery.notes.append(
                    f"item #{plan.index + 1}: Telegram could not fetch the url, uploaded it instead"
                )
                logger.info("url delivery failed for %s: %s", plan.url, exc)

        try:
            await self._send_by_server(
                chat_id,
                plan,
                caption=caption,
                reply_markup=reply_markup,
                parse_mode="HTML",
                **kwargs,
            )
            delivery.sent += 1
            delivery.bytes_sent += plan.size or 0
            return delivery
        except TelegramForbiddenError:
            raise
        except TelegramAPIError as exc:
            delivery.errors.append(f"item #{plan.index + 1}: {exc}")
            return delivery
        except Exception as exc:  # noqa: BLE001 - network fetch problems
            delivery.errors.append(f"item #{plan.index + 1}: {type(exc).__name__}: {exc}")
            return delivery

    async def _send_by_url(
        self,
        chat_id: int,
        plan: PlannedItem,
        *,
        caption: Optional[str],
        reply_markup: Optional[InlineKeyboardMarkup] = None,
        **kwargs,
    ) -> Message:
        url = plan.url or ""
        if plan.send_as == SendAs.PHOTO:
            return await self._bot.send_photo(
                chat_id, photo=url, caption=caption, reply_markup=reply_markup, **kwargs
            )
        if plan.send_as == SendAs.VIDEO:
            return await self._bot.send_video(
                chat_id,
                video=url,
                caption=caption,
                supports_streaming=True,
                reply_markup=reply_markup,
                **kwargs,
            )
        if plan.send_as == SendAs.ANIMATION:
            return await self._bot.send_animation(
                chat_id, animation=url, caption=caption, reply_markup=reply_markup, **kwargs
            )
        if plan.send_as == SendAs.AUDIO:
            return await self._bot.send_audio(
                chat_id, audio=url, caption=caption, reply_markup=reply_markup, **kwargs
            )
        return await self._bot.send_document(
            chat_id, document=url, caption=caption, reply_markup=reply_markup, **kwargs
        )

    async def _send_by_server(
        self,
        chat_id: int,
        plan: PlannedItem,
        *,
        caption: Optional[str],
        reply_markup: Optional[InlineKeyboardMarkup] = None,
        **kwargs,
    ) -> Message:
        if not plan.url:
            raise ValueError(f"item #{plan.index + 1} has no url to fetch")
        data = await self._downloader.fetch(plan.url, max_bytes=self.upload_limit)
        filename = filename_of(plan.url, fallback=f"{plan.send_as}_{plan.index + 1}")
        file = BufferedInputFile(data, filename=filename)
        return await self._send_file(
            chat_id,
            plan.send_as,
            file,
            caption=caption,
            reply_markup=reply_markup,
            **kwargs,
        )

    async def _send_file(
        self,
        chat_id: int,
        send_as: str,
        file: object,
        *,
        caption: Optional[str],
        reply_markup: Optional[InlineKeyboardMarkup] = None,
        **kwargs,
    ) -> Message:
        if send_as == SendAs.PHOTO:
            return await self._bot.send_photo(
                chat_id, photo=file, caption=caption, reply_markup=reply_markup, **kwargs
            )
        if send_as == SendAs.VIDEO:
            return await self._bot.send_video(
                chat_id,
                video=file,
                caption=caption,
                supports_streaming=True,
                reply_markup=reply_markup,
                **kwargs,
            )
        if send_as == SendAs.ANIMATION:
            return await self._bot.send_animation(
                chat_id, animation=file, caption=caption, reply_markup=reply_markup, **kwargs
            )
        if send_as == SendAs.AUDIO:
            return await self._bot.send_audio(
                chat_id, audio=file, caption=caption, reply_markup=reply_markup, **kwargs
            )
        return await self._bot.send_document(
            chat_id, document=file, caption=caption, reply_markup=reply_markup, **kwargs
        )

    # ----------------------------------------------------------------- album
    async def _send_album(
        self,
        chat_id: int,
        plans: Sequence[PlannedItem],
        *,
        caption: Optional[str],
        reply_to: Optional[int],
        thread_id: Optional[int],
        parse_mode: Optional[str] = None,
    ) -> Delivery:
        delivery = Delivery()
        if len(plans) < 2:
            for plan in plans:
                delivery.merge(
                    await self._send_single(
                        chat_id, plan, caption=caption, reply_to=reply_to, thread_id=thread_id
                    )
                )
                caption = None
            return delivery

        kwargs = self._reply(reply_to, thread_id)
        try:
            await self._guard(
                self._send_album_by_url,
                chat_id,
                plans,
                caption=caption,
                parse_mode=parse_mode,
                **kwargs,
            )
            delivery.sent += len(plans)
            delivery.bytes_sent += sum(plan.size or 0 for plan in plans)
            return delivery
        except TelegramForbiddenError:
            raise
        except (TelegramBadRequest, TelegramAPIError) as exc:
            if not is_url_failure(exc):
                delivery.errors.append(f"album: {exc}")
                return delivery
            delivery.notes.append(
                "Telegram could not fetch the album urls, uploaded the files instead"
            )
            logger.info("album url delivery failed: %s", exc)

        for plan in plans:
            delivery.merge(
                await self._send_single(
                    chat_id,
                    plan,
                    caption=caption,
                    reply_to=reply_to,
                    thread_id=thread_id,
                    reply_markup=None,
                )
            )
            caption = None
        return delivery

    async def _send_album_by_url(
        self,
        chat_id: int,
        plans: Sequence[PlannedItem],
        *,
        caption: Optional[str],
        parse_mode: Optional[str] = None,
        **kwargs,
    ) -> list[Message]:
        media = []
        for position, plan in enumerate(plans):
            cls = _MEDIA_CLASSES.get(plan.send_as, InputMediaPhoto)
            media.append(
                cls(
                    media=plan.url or "",
                    caption=caption if position == 0 else None,
                    parse_mode=parse_mode if position == 0 else None,
                )
            )
        return await self._bot.send_media_group(chat_id, media=media, **kwargs)

    # ------------------------------------------------------- processed files
    async def send_files(
        self,
        chat_id: int,
        files: Sequence[DownloadedFile],
        *,
        caption: Optional[str] = None,
        reply_to: Optional[int] = None,
        thread_id: Optional[int] = None,
        reply_markup: Optional[InlineKeyboardMarkup] = None,
    ) -> Delivery:
        """Upload files the processing queue produced (muxed video, audio...)."""
        delivery = Delivery()
        for position, file in enumerate(files):
            text = caption if position == 0 else None
            kwargs = self._reply(reply_to, thread_id)
            try:
                await self._guard(
                    self._send_downloaded,
                    chat_id,
                    file,
                    text,
                    reply_markup=reply_markup if position == 0 else None,
                    parse_mode="HTML",
                    **kwargs,
                )
                delivery.sent += 1
                delivery.bytes_sent += file.size
            except TelegramForbiddenError:
                raise
            except TelegramAPIError as exc:
                delivery.errors.append(f"{file.filename}: {exc}")
            except Exception as exc:  # noqa: BLE001 - defensive
                delivery.errors.append(f"{file.filename}: {type(exc).__name__}: {exc}")
        return delivery

    async def _send_downloaded(
        self,
        chat_id: int,
        file: DownloadedFile,
        caption: Optional[str],
        *,
        reply_markup: Optional[InlineKeyboardMarkup] = None,
        **kwargs,
    ) -> Message:
        source = _file_input(file)
        kind = str(file.kind)
        meta = file.format
        if kind == "video":
            return await self._bot.send_video(
                chat_id,
                video=source,
                caption=caption,
                supports_streaming=True,
                reply_markup=reply_markup,
                width=meta.width if meta else None,
                height=meta.height if meta else None,
                **kwargs,
            )
        if kind == "audio":
            return await self._bot.send_audio(
                chat_id,
                audio=source,
                caption=caption,
                reply_markup=reply_markup,
                **kwargs,
            )
        if kind == "image":
            return await self._bot.send_photo(
                chat_id,
                photo=source,
                caption=caption,
                reply_markup=reply_markup,
                **kwargs,
            )
        if kind == "gif":
            return await self._bot.send_animation(
                chat_id,
                animation=source,
                caption=caption,
                reply_markup=reply_markup,
                **kwargs,
            )
        return await self._bot.send_document(
            chat_id,
            document=source,
            caption=caption,
            reply_markup=reply_markup,
            **kwargs,
        )


def _file_input(file: DownloadedFile):
    """A Telegram-ready input for a downloaded file (disk or memory)."""
    if file.data is not None:
        return BufferedInputFile(file.data, filename=file.filename)
    if file.path is not None:
        return FSInputFile(str(file.path), filename=file.filename)
    raise ValueError(f"{file.filename} has neither bytes nor a path")


def _chunks(items: Sequence[PlannedItem], size: int) -> list[list[PlannedItem]]:
    return [list(items[index : index + size]) for index in range(0, len(items), size)]


__all__ = ["Delivery", "MediaSender", "is_url_failure"]
