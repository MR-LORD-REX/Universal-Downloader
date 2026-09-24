"""Entry point for the Telegram downloader bot.

Runs the aiogram dispatcher and the FastAPI app in one process:

* ``BOT_MODE=polling`` (default) - the dispatcher long-polls Telegram as a
  background task while FastAPI serves `/health` and `/api/*`.
* ``BOT_MODE=webhook`` - FastAPI exposes `POST <WEBHOOK_PATH>` and Telegram
  pushes updates to it.

```bash
python main.py                    # polling on :8080
python main.py --mode webhook     # webhook on :8080
python main.py --set-commands     # publish /commands and exit
```
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
from contextlib import asynccontextmanager
from typing import Any, Optional

import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommandScopeDefault,
    MenuButtonCommands,
    Update,
)
from fastapi import FastAPI, Header, HTTPException, Request, Response
from sqlalchemy import text

from bot.app import AppContext
from bot.config import ROOT, Settings, get_settings, settings
from bot.db.base import db
from bot.db.migrate import upgrade as run_migrations
from bot.handlers import routers
from bot.middlewares import ContextMiddleware, DatabaseMiddleware, UserMiddleware
from bot.services.commands import register_commands

logger = logging.getLogger("bot")


# --------------------------------------------------------------------- setup
def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
        force=True,
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


def build_bot(config: Settings) -> Bot:
    if not config.bot_token:
        raise RuntimeError(
            "BOT_TOKEN is not set. Copy .env.example to .env and paste the token from @BotFather."
        )
    return Bot(
        token=config.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def build_dispatcher(ctx: AppContext) -> Dispatcher:
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.update.outer_middleware(ContextMiddleware(ctx))
    dispatcher.update.outer_middleware(DatabaseMiddleware())
    for router in routers:
        dispatcher.include_router(router)
    dispatcher.message.middleware(UserMiddleware())
    dispatcher.callback_query.middleware(UserMiddleware())
    return dispatcher


async def prepare_database(config: Settings) -> None:
    if config.auto_migrate:
        logger.info("applying database migrations")
        await run_migrations("head")
    else:
        await db.create_all()


# ------------------------------------------------------------------ web app
def build_app(bot: Bot, dispatcher: Dispatcher, ctx: AppContext, config: Settings) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await prepare_database(config)
        await ctx.start()
        if config.register_commands:
            await register_commands(bot, admin_ids=config.all_admin_ids)
        with contextlib.suppress(TelegramAPIError):
            await bot.set_chat_menu_button(menu_button=MenuButtonCommands())

        poller: Optional[asyncio.Task] = None
        if config.bot_mode == "webhook":
            if not config.webhook_url:
                raise RuntimeError("WEBHOOK_URL must be set when BOT_MODE=webhook")
            await bot.set_webhook(
                url=config.webhook_target,
                secret_token=config.webhook_secret or None,
                drop_pending_updates=config.drop_pending_updates,
                allowed_updates=dispatcher.resolve_used_update_types(),
            )
            logger.info("webhook set to %s", config.webhook_target)
        else:
            await bot.delete_webhook(drop_pending_updates=config.drop_pending_updates)
            poller = asyncio.create_task(
                dispatcher.start_polling(
                    bot,
                    handle_signals=False,
                    allowed_updates=dispatcher.resolve_used_update_types(),
                ),
                name="telegram-polling",
            )
            logger.info("long polling started")

        app.state.poller = poller
        try:
            yield
        finally:
            if poller is not None:
                poller.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await poller
            if config.bot_mode == "webhook":
                with contextlib.suppress(TelegramAPIError):
                    await bot.delete_webhook()
            await ctx.stop()
            await dispatcher.storage.close()
            await bot.session.close()
            await db.dispose()
            logger.info("shutdown complete")

    app = FastAPI(
        title=f"{config.bot_name} downloader bot",
        version="1.0.0",
        lifespan=lifespan,
    )

    @app.get("/health")
    async def health() -> dict[str, Any]:
        database_ok = True
        try:
            async with db.session() as session:
                await session.execute(text("select 1"))
        except Exception:  # noqa: BLE001 - health must not raise
            database_ok = False
        return {
            "status": "ok" if database_ok else "degraded",
            "mode": config.bot_mode,
            "bot": ctx.bot_username,
            "platforms": list(ctx.registry.enabled),
            "database": database_ok,
            "queues": ctx.queues.snapshot(),
        }

    @app.get("/api/stats")
    async def api_stats() -> dict[str, Any]:
        from bot.db import repo

        async with db.session() as session:
            return await repo.analytics_overview(session, days=7)

    @app.get("/api/platforms")
    async def api_platforms() -> list[dict[str, Any]]:
        return [
            {
                "platform": snapshot.platform,
                "enabled": snapshot.enabled,
                "quality": snapshot.quality,
                "max_media_size_bytes": snapshot.max_media_size_bytes,
            }
            for snapshot in ctx.registry.all()
        ]

    @app.post(config.webhook_path)
    async def telegram_webhook(
        request: Request,
        x_telegram_bot_api_secret_token: Optional[str] = Header(default=None),
    ) -> Response:
        if config.webhook_secret and x_telegram_bot_api_secret_token != config.webhook_secret:
            raise HTTPException(status_code=403, detail="invalid secret token")
        payload = await request.json()
        update = Update.model_validate(payload, context={"bot": bot})
        await dispatcher.feed_update(bot, update)
        return Response(status_code=200)

    return app


# ---------------------------------------------------------------------- run
class _Server(uvicorn.Server):
    """uvicorn server that lets an external loop own the lifespan."""

    def install_signal_handlers(self) -> None:  # pragma: no cover - intentional
        pass


async def serve(bot: Bot, dispatcher: Dispatcher, ctx: AppContext, config: Settings) -> None:
    app = build_app(bot, dispatcher, ctx, config)
    server = _Server(
        uvicorn.Config(
            app,
            host=config.host,
            port=config.port,
            log_level=config.log_level.lower(),
            access_log=False,
        )
    )
    await server.serve()


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Telegram downloader bot")
    parser.add_argument("--mode", choices=("polling", "webhook"), default=None)
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--set-commands", action="store_true", help="publish the command list and exit")
    parser.add_argument("--migrate", action="store_true", help="apply database migrations and exit")
    return parser.parse_args(argv)


async def _set_commands_and_exit(config: Settings) -> None:
    bot = build_bot(config)
    try:
        await register_commands(bot, admin_ids=config.all_admin_ids)
        logger.info("commands published")
    finally:
        await bot.session.close()


def main(argv: Optional[list[str]] = None) -> None:
    args = parse_args(argv)
    config = get_settings()
    if args.mode:
        config.bot_mode = args.mode  # type: ignore[assignment]
    if args.host:
        config.host = args.host
    if args.port:
        config.port = args.port
    configure_logging(config.log_level)

    if args.migrate:
        asyncio.run(run_migrations("head"))
        logger.info("migrations applied")
        return
    if args.set_commands:
        asyncio.run(_set_commands_and_exit(config))
        return

    bot = build_bot(config)
    ctx = AppContext(bot=bot, settings=config)
    dispatcher = build_dispatcher(ctx)
    try:
        asyncio.run(serve(bot, dispatcher, ctx, config))
    except KeyboardInterrupt:  # pragma: no cover - interactive
        logger.info("interrupted")


app: Optional[FastAPI] = None
"""Set by ``uvicorn main:app --factory`` style deployments; see :func:`build_app`."""


if __name__ == "__main__":
    main()
