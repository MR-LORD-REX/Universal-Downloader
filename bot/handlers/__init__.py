"""aiogram routers, registered in order by :func:`bot.main.build_dispatcher`."""

from . import admin, links, start, suggestion

routers = (start.router, suggestion.router, admin.router, links.router)

__all__ = ["routers"]
