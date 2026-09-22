from __future__ import annotations

import asyncio
import logging
from typing import Any, Coroutine

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant


def create_logged_task(
    hass: HomeAssistant,
    coro: Coroutine[Any, Any, Any],
    *,
    logger: logging.Logger,
    message: str,
    task_set: set[asyncio.Task] | None = None,
    entry: ConfigEntry | None = None,
) -> asyncio.Task:
    """Create a background task that is tracked and logs failures.

    The task is created as a Home Assistant *background* task so it neither
    blocks the bootstrap "wait for setup tasks" phase nor delays platform
    setup. When ``entry`` is given the task is bound to that config entry and
    cancelled automatically when the entry unloads.

    Keeps a reference (via task_set) so the task cannot be garbage-collected
    mid-flight, and surfaces exceptions instead of swallowing them.

    Must be called from the event loop thread; Home Assistant verifies this
    and raises otherwise. Callers running in an executor thread must hop into
    the loop first (e.g. ``hass.loop.call_soon_threadsafe``) and create the
    coroutine there.
    """
    if entry is not None:
        task = entry.async_create_background_task(hass, coro, name=message)
    else:
        task = hass.async_create_background_task(coro, name=message)
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
