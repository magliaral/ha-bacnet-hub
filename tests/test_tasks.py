"""Tests for helpers.tasks.create_logged_task."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import MagicMock

import pytest

from custom_components.bacnet_hub.helpers.tasks import cancel_tasks, create_logged_task


class _FakeHass:
    """Minimal hass stand-in that records background task creation."""

    def __init__(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.background_calls: list[str] = []
        self.async_create_task = MagicMock(
            side_effect=AssertionError("setup-tracked async_create_task must not be used")
        )

    def async_create_background_task(self, coro, name, eager_start=True):
        self.background_calls.append(name)
        return self.loop.create_task(coro, name=name)


class _FakeEntry:
    def __init__(self) -> None:
        self.background_calls: list[str] = []

    def async_create_background_task(self, hass, coro, name, eager_start=True):
        self.background_calls.append(name)
        return hass.loop.create_task(coro, name=name)


async def _ok() -> str:
    await asyncio.sleep(0)
    return "done"


async def _boom() -> None:
    await asyncio.sleep(0)
    raise RuntimeError("kaboom")


async def _forever() -> None:
    while True:
        await asyncio.sleep(3600)


async def _settle() -> None:
    # Let done-callbacks scheduled via call_soon run.
    for _ in range(3):
        await asyncio.sleep(0)


async def test_uses_hass_background_task_and_tracks_task_set():
    hass = _FakeHass()
    logger = MagicMock(spec=logging.Logger)
    task_set: set[asyncio.Task] = set()

    task = create_logged_task(hass, _ok(), logger=logger, message="ok task", task_set=task_set)

    assert hass.background_calls == ["ok task"]
    hass.async_create_task.assert_not_called()
    assert task in task_set
    assert await task == "done"
    await _settle()
    assert task_set == set()
    logger.warning.assert_not_called()


async def test_uses_entry_background_task_when_entry_given():
    hass = _FakeHass()
    entry = _FakeEntry()
    logger = MagicMock(spec=logging.Logger)

    task = create_logged_task(hass, _ok(), logger=logger, message="entry task", entry=entry)

    assert entry.background_calls == ["entry task"]
    assert hass.background_calls == []
    assert await task == "done"


async def test_logs_exception_once():
    hass = _FakeHass()
    logger = MagicMock(spec=logging.Logger)

    task = create_logged_task(hass, _boom(), logger=logger, message="boom task")
    with pytest.raises(RuntimeError):
        await task
    await _settle()

    logger.warning.assert_called_once()
    args, kwargs = logger.warning.call_args
    assert args[1] == "boom task"
    assert isinstance(args[2], RuntimeError)
    assert isinstance(kwargs["exc_info"], RuntimeError)


async def test_cancelled_task_is_not_logged_and_removed_from_set():
    hass = _FakeHass()
    logger = MagicMock(spec=logging.Logger)
    task_set: set[asyncio.Task] = set()

    task = create_logged_task(
        hass, _forever(), logger=logger, message="loop task", task_set=task_set
    )
    assert task in task_set

    cancel_tasks(task_set)
    with pytest.raises(asyncio.CancelledError):
        await task
    await _settle()

    assert task.cancelled()
    assert task_set == set()
    logger.warning.assert_not_called()
