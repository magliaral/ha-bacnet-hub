from __future__ import annotations

import asyncio
import logging
from typing import Any, Coroutine

from homeassistant.core import HomeAssistant


def create_logged_task(
    hass: HomeAssistant,
    coro: Coroutine[Any, Any, Any],
    *,
    logger: logging.Logger,
    message: str,
    task_set: set[asyncio.Task] | None = None,
) -> asyncio.Task:
    """Create a background task that is tracked and logs failures.

    Keeps a reference (via task_set) so the task cannot be garbage-collected
    mid-flight, and surfaces exceptions instead of swallowing them.
    """
    task = hass.async_create_task(coro)
    if task_set is not None:
        task_set.add(task)

    def _done(done_task: asyncio.Task) -> None:
        if task_set is not None:
            task_set.discard(done_task)
        if done_task.cancelled():
            return
        err = done_task.exception()
        if err is not None:
            logger.warning("%s failed: %s", message, err, exc_info=err)

    task.add_done_callback(_done)
    return task


def cancel_tasks(task_set: set[asyncio.Task]) -> None:
    """Cancel all pending tasks in the set and clear it."""
    for task in list(task_set):
        if not task.done():
            task.cancel()
    task_set.clear()
