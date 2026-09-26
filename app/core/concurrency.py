"""Helpers for calling blocking drivers from async request handlers."""

import asyncio
from collections.abc import Callable
from functools import partial
from typing import Any


async def run_blocking[T](function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run a synchronous call such as a SQLAlchemy session off the event loop.

    The persistence layer is synchronous SQLAlchemy and the Redis-independent
    memory stores are synchronous too. Awaiting them directly inside ``async def``
    handlers would block every other request on the same worker, so the call is
    handed to a thread instead.
    """
    return await asyncio.to_thread(partial(function, *args, **kwargs))
