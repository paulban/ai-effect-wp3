"""Shared fixtures for the Human-AI Interaction Testing test suite.

Puts both the service directory and the ``use-cases`` directory on the import
path, which is the arrangement the container reproduces: the shared control
plane is importable as ``common`` and this service's own code as ``hai``.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

SERVICE_DIRECTORY = Path(__file__).resolve().parents[1]
USE_CASES_DIRECTORY = SERVICE_DIRECTORY.parents[2]

for import_path in (str(SERVICE_DIRECTORY), str(USE_CASES_DIRECTORY)):
    if import_path not in sys.path:
        sys.path.insert(0, import_path)

# Signing secret for every test that issues or verifies a session link. Set
# before any import of the token module so its length validation passes.
os.environ.setdefault("HAI_SESSION_TOKEN_SECRET", "test-secret-" + "0" * 32)

from common.artifacts import FileArtifactStore  # noqa: E402
from hai.session_manager import SessionManager  # noqa: E402
from hai.session_service import SessionService  # noqa: E402
from hai.slot_pool import SlotPool  # noqa: E402
from tests.fake_redis import FakeRedis  # noqa: E402

# Number of slots the tests exercise, matching the deployment default so the
# exhaustion tests reflect what a real second participant would hit.
TEST_SLOT_COUNT = 2


@pytest.fixture
def fake_redis() -> FakeRedis:
    """An empty in-memory Redis double."""
    return FakeRedis()


@pytest.fixture
def session_manager(fake_redis: FakeRedis) -> SessionManager:
    """A session store backed by the fake client."""
    return SessionManager(fake_redis)


@pytest.fixture
def slot_pool(fake_redis: FakeRedis) -> SlotPool:
    """A two-slot pool sharing the session store's client."""
    return SlotPool(fake_redis, slot_count=TEST_SLOT_COUNT)


@pytest.fixture
def artifact_store() -> FileArtifactStore:
    """An artifact store in a throwaway directory."""
    return FileArtifactStore(tempfile.mkdtemp(prefix="hai-artifacts-"))


@pytest.fixture
def session_service(
    session_manager: SessionManager,
    slot_pool: SlotPool,
    artifact_store: FileArtifactStore,
) -> SessionService:
    """
    A session service whose slot HTTP calls are stubbed out.

    Configuring a slot means talking to two containers over HTTP. Those calls
    are replaced here so the lifecycle can be tested without them; the calls
    themselves are covered by the container-level checks in the spec's test
    scenarios, which need a running stack.
    """
    service = SessionService(
        session_manager,
        slot_pool,
        artifact_store,
        public_base_url="https://node.test",
    )

    service.configured_slots = []
    service.reset_slots = []

    def record_configure(slot, session_id, spec, session_token):
        service.configured_slots.append((slot.index, session_id, session_token))

    def record_reset(slot):
        service.reset_slots.append(slot.index)

    service._configure_slot = record_configure  # noqa: SLF001 - test seam
    service._reset_slot = record_reset  # noqa: SLF001 - test seam
    return service


def token_from_url(session_url: str) -> str:
    """
    Extract the session token from a participant URL.

    Args:
        session_url: URL produced by the service.

    Returns:
        The token query parameter's value.
    """
    return session_url.split("?t=", 1)[1]
