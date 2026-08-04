"""An in-memory stand-in for the Redis client the session layer expects.

Only the operations this service actually uses are implemented: string
get/set with NX and EX, set membership, server-side scripts, and locks. That is
deliberate — a double that quietly accepts calls the real client would reject
lets bugs through, so anything unimplemented raises rather than returning a
plausible-looking default.

Values are stored as ``str`` because the production client is built with
``decode_responses=True``.

Used by the session store, slot pool and session operation tests.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator


class FakeRedisLock:
    """A re-entrant-free lock keyed by name, matching redis-py's context manager."""

    def __init__(self, owner: FakeRedis, name: str, timeout: float | None) -> None:
        """
        Args:
            owner: Client holding the lock registry.
            name: Lock key.
            timeout: Accepted for signature compatibility; a real deployment
                relies on it to break locks held by dead processes, which
                cannot happen in a single-process test.
        """
        self._owner = owner
        self._name = name
        self._timeout = timeout

    def __enter__(self) -> FakeRedisLock:
        """Acquire the underlying threading lock."""
        self._owner.lock_registry.setdefault(self._name, threading.Lock()).acquire()
        return self

    def __exit__(self, *exception_details: Any) -> None:
        """Release the underlying threading lock."""
        self._owner.lock_registry[self._name].release()


class FakeRedis:
    """Minimal in-memory Redis double.

    Example:
        >>> client = FakeRedis()
        >>> client.set("key", "value", nx=True)
        True
        >>> client.set("key", "other", nx=True) is None
        True
        >>> client.get("key")
        'value'
    """

    def __init__(self) -> None:
        """Create an empty store."""
        self.strings: dict[str, str] = {}
        self.expiries: dict[str, float] = {}
        self.sets: dict[str, set[str]] = {}
        self.lock_registry: dict[str, threading.Lock] = {}
        self._mutex = threading.Lock()

    # -- string operations -------------------------------------------------

    def set(
        self,
        key: str,
        value: str,
        nx: bool = False,
        ex: int | None = None,
    ) -> bool | None:
        """
        Set a key, optionally only if absent.

        Args:
            key: Key to write.
            value: Value to store.
            nx: When true, do nothing if the key already exists.
            ex: Expiry in seconds.

        Returns:
            True when the write happened, None when NX prevented it — matching
            redis-py, whose falsy return is None rather than False.
        """
        with self._mutex:
            self._expire_if_due(key)
            if nx and key in self.strings:
                return None

            self.strings[key] = value
            if ex is not None:
                self.expiries[key] = time.time() + ex
            else:
                self.expiries.pop(key, None)
            return True

    def get(self, key: str) -> str | None:
        """Return a key's value, or None when absent or expired."""
        with self._mutex:
            self._expire_if_due(key)
            return self.strings.get(key)

    def delete(self, key: str) -> int:
        """Delete a key, returning how many keys were removed."""
        with self._mutex:
            self.expiries.pop(key, None)
            return 1 if self.strings.pop(key, None) is not None else 0

    # -- set operations ----------------------------------------------------

    def sadd(self, key: str, *members: str) -> int:
        """Add members to a set, returning how many were new."""
        with self._mutex:
            target = self.sets.setdefault(key, set())
            new_members = {member for member in members if member not in target}
            target.update(new_members)
            return len(new_members)

    def smembers(self, key: str) -> set[str]:
        """Return a copy of a set's members."""
        with self._mutex:
            return set(self.sets.get(key, set()))

    def srem(self, key: str, *members: str) -> int:
        """Remove members from a set, returning how many were removed."""
        with self._mutex:
            target = self.sets.get(key, set())
            removed = {member for member in members if member in target}
            target.difference_update(removed)
            return len(removed)

    # -- scripts and locks -------------------------------------------------

    def register_script(self, script_source: str) -> Callable[..., int]:
        """
        Return a callable emulating the one Lua script this service registers.

        The script is "delete this key if it still holds this value". Rather
        than interpret Lua, the source is checked to be that script and an
        equivalent atomic operation is returned; anything else raises, so a new
        script cannot silently do nothing in tests.

        Args:
            script_source: Lua source registered by the caller.

        Returns:
            Callable taking ``keys`` and ``args``, returning 1 on delete.

        Raises:
            NotImplementedError: If the script is not the recognised one.
        """
        if "del" not in script_source or "get" not in script_source:
            raise NotImplementedError(f"FakeRedis cannot emulate script: {script_source!r}")

        def delete_if_value_matches(keys: list[str], args: list[str]) -> int:
            with self._mutex:
                if self.strings.get(keys[0]) == args[0]:
                    del self.strings[keys[0]]
                    self.expiries.pop(keys[0], None)
                    return 1
                return 0

        return delete_if_value_matches

    def lock(self, name: str, timeout: float | None = None) -> FakeRedisLock:
        """Return a lock object usable as a context manager."""
        return FakeRedisLock(self, name, timeout)

    # -- helpers -----------------------------------------------------------

    def _expire_if_due(self, key: str) -> None:
        """Drop a key whose expiry has passed. Caller must hold the mutex."""
        expires_at = self.expiries.get(key)
        if expires_at is not None and expires_at <= time.time():
            self.strings.pop(key, None)
            self.expiries.pop(key, None)

    @contextmanager
    def frozen_at(self, timestamp: float) -> Iterator[None]:
        """
        Not implemented — present so tests fail loudly instead of silently.

        Raises:
            NotImplementedError: Always.
        """
        raise NotImplementedError("FakeRedis does not simulate clock control")
        yield  # pragma: no cover
