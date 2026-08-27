"""Shared optimistic-command orchestration for HYMER Connect entities.

Every commandable entity sets an OPTIMISTIC value immediately, then clears it
once the SCU readback confirms it. If a command is DROPPED (e.g. a BLE write
failed, fell back to cloud, and that cloud command was not applied), the SCU
keeps reporting the OLD value, so a "clear-only-on-match" reconciliation would
leave the entity stuck on the wrong optimistic value forever AND never re-send
the command. Real-world danger: turning the water pump OFF, the OFF being
dropped, and the pump running dry.

This mixin centralises the two protections first proven in ``light.py`` so all
commandable entities behave identically across the dual BLE→cloud path:

1. TTL self-heal — an optimistic value the SCU never confirms is dropped after
   ``OPTIMISTIC_STATE_TTL`` so the real readback wins.
2. Verify-and-retry — after each command, wait ``COMMAND_VERIFY_DELAY`` for a
   confirming readback; if none arrives (and confirmation is possible) re-send
   the exact same command ONCE.

The mixin only orchestrates timing, skip conditions and task scheduling. Each
entity supplies three tiny hooks describing its own optimistic state.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

_LOGGER = logging.getLogger(__name__)


class OptimisticCommandMixin:
    """Add TTL self-heal + one-shot verify-and-retry to a commandable entity.

    Include it in the entity's base list (it overrides none of Home Assistant's
    entity methods, so it is placed last to keep ``super().__init__`` resolving
    to the ``CoordinatorEntity`` base). The concrete class must implement
    ``_has_pending_optimistic``, ``_command_confirmed`` and
    ``_clear_optimistic``, and call ``_init_optimistic()`` from ``__init__``.
    """

    # An unconfirmed optimistic value is dropped after this many seconds.
    OPTIMISTIC_STATE_TTL = 20.0
    # Wait this long for a confirming readback before re-sending a command once.
    COMMAND_VERIFY_DELAY = 8.0

    # Provided by the concrete CoordinatorEntity subclass at runtime.
    coordinator: Any

    def _init_optimistic(self) -> None:
        """Initialise mixin state. Call from each entity ``__init__``."""
        self._optimistic_set_at: float = 0.0
        self._verify_task: asyncio.Task | None = None
        self._resend: Callable[[], Awaitable[Any]] | None = None

    def _note_command(self, resend: Callable[[], Awaitable[Any]]) -> None:
        """Record a just-sent command and (re)arm the verify watchdog.

        Call AFTER sending the command and setting the optimistic fields, with a
        zero-arg async callable that re-issues the EXACT same command.
        """
        self._optimistic_set_at = time.monotonic()
        self._resend = resend
        if self._verify_task and not self._verify_task.done():
            self._verify_task.cancel()
        self._verify_task = asyncio.ensure_future(self._verify_and_retry())

    def _optimistic_ttl_expired(self) -> bool:
        """True when the last optimistic value is older than the TTL."""
        return (
            self._optimistic_set_at > 0
            and time.monotonic() - self._optimistic_set_at > self.OPTIMISTIC_STATE_TTL
        )

    def _skip_command_retry(self) -> bool:
        """True when a retry could not be confirmed (12V off / frozen SCU)."""
        return (
            self.coordinator.data_silence_seconds
            > self.coordinator.unavailable_silence_threshold
            or self.coordinator.scu_frozen
        )

    def _label(self) -> str:
        """Best-effort identifier for log lines."""
        return getattr(self, "entity_id", None) or getattr(self, "_attr_unique_id", "?")

    # --- hooks each entity implements -----------------------------------
    def _has_pending_optimistic(self) -> bool:
        """True while any optimistic field is set (awaiting confirmation)."""
        raise NotImplementedError

    def _command_confirmed(self) -> bool:
        """True when the REAL SCU readback matches the pending optimistic value."""
        raise NotImplementedError

    def _clear_optimistic(self) -> None:
        """Clear ALL optimistic fields so the real readback wins."""
        raise NotImplementedError

    async def _verify_and_retry(self) -> None:
        """Re-send the last command once if the SCU never confirms it.

        A dropped command (failed BLE write that fell back to cloud but was not
        applied) is otherwise only corrected visually by the TTL. Skip retrying
        when the SCU cannot confirm anyway (12V off / data silent) or is frozen.
        """
        try:
            await asyncio.sleep(self.COMMAND_VERIFY_DELAY)
            if not self._has_pending_optimistic():
                return  # already confirmed/cleared or superseded
            if self._command_confirmed():
                return  # confirmed by a real readback
            if self._skip_command_retry():
                return  # 12V off or hung SCU — a retry could not be confirmed
            if self._resend is None:
                return
            _LOGGER.info(
                "Command for %s not confirmed after %.0fs — re-sending once",
                self._label(), self.COMMAND_VERIFY_DELAY,
            )
            await self._resend()
            self._optimistic_set_at = time.monotonic()  # restart self-heal TTL
            await asyncio.sleep(self.COMMAND_VERIFY_DELAY)
            if self._has_pending_optimistic() and not self._command_confirmed():
                _LOGGER.info(
                    "Command for %s still unconfirmed after retry — leaving the "
                    "real SCU state to win",
                    self._label(),
                )
        except asyncio.CancelledError:
            pass

    async def _cancel_verify(self) -> None:
        """Cancel a pending verify watchdog (on removal)."""
        if self._verify_task and not self._verify_task.done():
            self._verify_task.cancel()
