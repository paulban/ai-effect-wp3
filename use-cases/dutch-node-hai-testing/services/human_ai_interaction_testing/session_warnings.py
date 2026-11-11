"""
Session warnings — a thread-safe, capped log of what went wrong but did not fail.

A broken algorithm raises on every recommendation request, which in a long
session would mean thousands of identical warnings in the result. The log keeps
the first ``MAXIMUM_RECORDED_WARNINGS`` and counts the rest.

Written to from request threads (the agent endpoint) and read by the session
job thread, hence the lock.

Spec coverage: FR-19, section 5.7 (``metadata.warnings``)
"""

from __future__ import annotations

import threading

from settings import MAXIMUM_RECORDED_WARNINGS


class SessionWarnings:
    """
    Collects warning messages for one session, keeping at most a fixed number.

    Example:
        >>> session_warnings = SessionWarnings(maximum_recorded=1)
        >>> session_warnings.add("first")
        >>> session_warnings.add("second")
        >>> session_warnings.as_list()
        ['first', '1 further warning(s) not recorded']
    """

    def __init__(self, maximum_recorded: int = MAXIMUM_RECORDED_WARNINGS) -> None:
        """
        Args:
            maximum_recorded: How many messages are kept verbatim.
        """
        self._maximum_recorded = maximum_recorded
        self._messages: list[str] = []
        self._dropped_count = 0
        self._lock = threading.Lock()

    def add(self, message: str) -> None:
        """
        Record one warning, or count it once the cap is reached.

        Args:
            message: One-line description.
        """
        with self._lock:
            if len(self._messages) < self._maximum_recorded:
                self._messages.append(message)
            else:
                self._dropped_count += 1

    def as_list(self) -> list[str]:
        """
        Return the recorded warnings, followed by a count of dropped ones if any.

        Returns:
            A new list, safe to serialise.
        """
        with self._lock:
            messages = list(self._messages)
            if self._dropped_count:
                messages.append(f"{self._dropped_count} further warning(s) not recorded")
            return messages
