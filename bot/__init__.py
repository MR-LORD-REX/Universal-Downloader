"""Telegram downloader bot built on top of the multi-platform downloader SDK.

The package is layered so each concern can be tested on its own::

    bot.config      every knob, read from the environment
    bot.db          SQLAlchemy models, repositories and Alembic migrations
    bot.services    downloader facade, queues, routing, sending, analytics
    bot.ui          Telegram message styling and per-platform descriptions
    bot.handlers    aiogram routers (links, start, suggestion, admin)
    bot.main        FastAPI app + aiogram dispatcher (polling or webhook)
"""

__version__ = "1.0.0"

__all__ = ["__version__"]
