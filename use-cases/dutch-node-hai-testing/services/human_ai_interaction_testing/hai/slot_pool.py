"""A fixed pool of session slots, reserved one participant at a time.

The simulator's engine is a process-level singleton, so one simulator container
serves one participant. Previously the service met that constraint by creating
a container per session through the Docker socket, which gave the front-line
HTTP service root-equivalent access to the host and bound each session to a
fixed host port — capping the study at one participant.

Instead, a fixed number of simulator and survey containers start with the stack
and stay up. Starting a session means *reserving* one of them and configuring
it over HTTP. Nothing creates containers at runtime, so nothing needs the
Docker socket, and concurrency is bounded by how many slots were declared
rather than by port assignments.

Reservation is atomic in Redis. Two ``StartHumanAISession`` calls arriving
together cannot be handed the same slot: acquisition is a ``SET ... NX``, which
either wins or does not.

Spec coverage: FR-06, FR-07, FR-08, FR-10, FR-11
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger(__name__)

# How many session slots the deployment declares. Each slot is one simulator
# container plus one survey container, both idle until reserved. The ceiling is
# memory: every idle simulator holds a loaded grid2op environment.
DEFAULT_SLOT_COUNT = 2

# Where a slot's containers are reachable on the shared Docker network. The
# index is substituted in, and the compose file must declare matching service
# names. These are internal addresses; participants never see them, they reach
# a slot through the proxy by session id.
DEFAULT_SIMULATOR_URL_TEMPLATE = "http://hai-slot-{index}-simulator:5000"
DEFAULT_SURVEY_URL_TEMPLATE = "http://hai-slot-{index}-survey:80"

# Redis key holding the session id currently occupying a slot. Absence of the
# key means the slot is free — the free state is represented by absence rather
# than by a sentinel value so that SET NX is exactly the reservation primitive.
SLOT_KEY_PATTERN = "hai:slot:{index}"

# Backstop lifetime on a reservation. Normal release happens when the session
# ends, fails or times out; this only matters if the control service dies
# between reserving a slot and recording the session, in which case the slot
# would otherwise stay reserved forever. Generously longer than any session.
DEFAULT_RESERVATION_TTL_SECONDS = 24 * 60 * 60

# Release a slot only if it still holds the session releasing it. Without the
# comparison, a late release from a timed-out session could free a slot that
# has already been handed to the next participant.
_RELEASE_IF_OWNED_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""


@dataclass(frozen=True)
class SessionSlot:
    """One reservable pair of session containers.

    Attributes:
        index: 1-based slot number, matching the compose service names.
        simulator_url: Base URL of this slot's simulator, on the internal
            network. Used to configure and reset it.
        survey_url: Base URL of this slot's survey wrapper, on the internal
            network.
    """

    index: int
    simulator_url: str
    survey_url: str


class SlotPool:
    """Reserves and releases session slots atomically.

    Example:
        >>> pool = SlotPool(redis_client, slot_count=2)
        >>> slot = pool.reserve("session-abc")
        >>> slot.index if slot else None
        1
        >>> pool.release("session-abc")
        True
    """

    def __init__(
        self,
        redis_client,
        slot_count: int = DEFAULT_SLOT_COUNT,
        simulator_url_template: str = DEFAULT_SIMULATOR_URL_TEMPLATE,
        survey_url_template: str = DEFAULT_SURVEY_URL_TEMPLATE,
        reservation_ttl_seconds: int = DEFAULT_RESERVATION_TTL_SECONDS,
    ) -> None:
        """
        Build a pool over an existing Redis connection.

        Args:
            redis_client: Redis client, expected to decode responses to str.
            slot_count: Number of declared slots. Must be at least 1 and must
                match the number of container pairs in the compose file.
            simulator_url_template: Format string with an ``{index}`` field.
            survey_url_template: Format string with an ``{index}`` field.
            reservation_ttl_seconds: Backstop expiry on a reservation.

        Raises:
            ValueError: If slot_count is less than 1.
        """
        if slot_count < 1:
            raise ValueError(f"slot_count must be at least 1, got {slot_count}")

        self._redis = redis_client
        self._slot_count = slot_count
        self._reservation_ttl_seconds = reservation_ttl_seconds
        self._slots = [
            SessionSlot(
                index=index,
                simulator_url=simulator_url_template.format(index=index),
                survey_url=survey_url_template.format(index=index),
            )
            for index in range(1, slot_count + 1)
        ]
        self._release_if_owned = redis_client.register_script(_RELEASE_IF_OWNED_SCRIPT)

    @property
    def slot_count(self) -> int:
        """Number of slots declared in this pool."""
        return self._slot_count

    def all_slots(self) -> list[SessionSlot]:
        """
        List every declared slot, reserved or not.

        Returns:
            Slots in index order.
        """
        return list(self._slots)

    def reserve(self, session_id: str) -> SessionSlot | None:
        """
        Reserve the lowest-numbered free slot for a session.

        Acquisition uses ``SET NX``, so concurrent callers contend on Redis
        rather than in this process: exactly one of them can win any given
        slot, and a loser simply tries the next one.

        Args:
            session_id: Session to bind the slot to.

        Returns:
            The reserved slot, or None when every slot is occupied — which the
            caller reports as RESOURCE_EXHAUSTED (FR-10).

        Raises:
            ValueError: If session_id is empty.
        """
        if not session_id:
            raise ValueError("session_id must not be empty")

        for slot in self._slots:
            acquired = self._redis.set(
                SLOT_KEY_PATTERN.format(index=slot.index),
                session_id,
                nx=True,
                ex=self._reservation_ttl_seconds,
            )
            if acquired:
                logger.info("Reserved slot %d for session %s", slot.index, session_id)
                return slot

        logger.warning(
            "No free slot for session %s; all %d slots are occupied",
            session_id,
            self._slot_count,
        )
        return None

    def release(self, session_id: str) -> bool:
        """
        Release whichever slot a session holds.

        Idempotent: releasing a session that holds no slot is a no-op returning
        False, so a timeout and a normal completion racing each other cannot
        double-free.

        Args:
            session_id: Session whose slot should be freed.

        Returns:
            True if a slot was actually released.
        """
        for slot in self._slots:
            released_count = self._release_if_owned(
                keys=[SLOT_KEY_PATTERN.format(index=slot.index)],
                args=[session_id],
            )
            if released_count:
                logger.info("Released slot %d held by session %s", slot.index, session_id)
                return True

        return False

    def slot_for_session(self, session_id: str) -> SessionSlot | None:
        """
        Find the slot a session currently holds.

        Scans the declared slots rather than keeping a reverse index. With a
        pool this small the scan is trivial, and it cannot disagree with the
        reservation keys the way a separately maintained index could.

        Args:
            session_id: Session to look up.

        Returns:
            The slot, or None if this session holds none.
        """
        for slot in self._slots:
            occupant = self._redis.get(SLOT_KEY_PATTERN.format(index=slot.index))
            if occupant == session_id:
                return slot

        return None

    def busy_count(self) -> int:
        """
        Count how many slots are currently reserved.

        Returns:
            Number of occupied slots, between 0 and ``slot_count``.
        """
        return sum(
            1
            for slot in self._slots
            if self._redis.get(SLOT_KEY_PATTERN.format(index=slot.index)) is not None
        )

    def occupancy(self) -> dict[int, str | None]:
        """
        Report which session occupies each slot.

        Returns:
            Slot index mapped to the occupying session id, or None when free.
            Intended for diagnostics and the reconciliation log.
        """
        return {
            slot.index: self._redis.get(SLOT_KEY_PATTERN.format(index=slot.index))
            for slot in self._slots
        }

    def reconcile(self, is_session_active: Callable[[str], bool]) -> list[int]:
        """
        Free slots whose session is no longer live.

        Run at startup. A control process killed mid-session leaves its
        reservations behind, and without this the pool would shrink by one slot
        every unclean restart until nothing could start (NFR-07).

        Args:
            is_session_active: Predicate answering whether a session id is
                still running. Sessions that are unknown or in a terminal phase
                are not active, and their slots are reclaimed.

        Returns:
            Indices of the slots that were freed.
        """
        reclaimed_slot_indices = []

        for slot_index, occupying_session_id in self.occupancy().items():
            if occupying_session_id is None:
                continue

            if is_session_active(occupying_session_id):
                continue

            self._release_if_owned(
                keys=[SLOT_KEY_PATTERN.format(index=slot_index)],
                args=[occupying_session_id],
            )
            reclaimed_slot_indices.append(slot_index)
            logger.info(
                "Reclaimed slot %d from inactive session %s",
                slot_index,
                occupying_session_id,
            )

        if not reclaimed_slot_indices:
            logger.info("Slot reconciliation found nothing to reclaim")

        return reclaimed_slot_indices


def build_slot_pool(redis_client) -> SlotPool:
    """
    Build the pool described by the process environment.

    Args:
        redis_client: Redis client shared with the session store.

    Returns:
        Configured pool.

    Raises:
        ValueError: If HAI_SESSION_SLOT_COUNT is not a positive integer.
    """
    raw_slot_count = os.environ.get("HAI_SESSION_SLOT_COUNT", str(DEFAULT_SLOT_COUNT))
    try:
        slot_count = int(raw_slot_count)
    except ValueError:
        raise ValueError(
            f"HAI_SESSION_SLOT_COUNT must be an integer, got {raw_slot_count!r}"
        )

    return SlotPool(
        redis_client,
        slot_count=slot_count,
        simulator_url_template=os.environ.get(
            "HAI_SLOT_SIMULATOR_URL_TEMPLATE", DEFAULT_SIMULATOR_URL_TEMPLATE
        ),
        survey_url_template=os.environ.get(
            "HAI_SLOT_SURVEY_URL_TEMPLATE", DEFAULT_SURVEY_URL_TEMPLATE
        ),
        reservation_ttl_seconds=int(
            os.environ.get("HAI_SLOT_RESERVATION_TTL_SECONDS", DEFAULT_RESERVATION_TTL_SECONDS)
        ),
    )
