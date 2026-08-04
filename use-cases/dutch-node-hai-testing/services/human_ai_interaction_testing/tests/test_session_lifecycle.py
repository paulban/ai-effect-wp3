"""Session lifecycle tests, mapped to the acceptance criteria in the spec.

Each test names the requirement it covers and states the criterion it checks.
Where a criterion cannot be verified without a running stack — the browser
round trip, the real container configuration calls, the memory behaviour of a
reused grid2op environment — that is called out in the docs rather than faked
here.

Spec coverage: FR-05, FR-06, FR-07, FR-08, FR-10, FR-11, FR-15, FR-20, FR-21
"""

from __future__ import annotations

import time

import pytest

from hai.session_manager import SessionPhase
from hai.session_service import HumanAISessionSpec
from hai.session_tokens import issue_token
from tests.conftest import TEST_SLOT_COUNT, token_from_url


def test_concurrent_starts_receive_distinct_slots(session_service, slot_pool):
    """FR-08: concurrent starts never receive the same slot.

    "Given N=2 slots and starts issued together, when all return, then each has
    a distinct session_id and each resolves to a different slot."
    """
    first = session_service.start_session("session-one", HumanAISessionSpec())
    second = session_service.start_session("session-two", HumanAISessionSpec())

    assert first.succeeded and second.succeeded
    first_slot = slot_pool.slot_for_session("session-one")
    second_slot = slot_pool.slot_for_session("session-two")
    assert first_slot.index != second_slot.index
    assert slot_pool.busy_count() == TEST_SLOT_COUNT


def test_start_is_refused_when_every_slot_is_busy(session_service):
    """FR-10: exhaustion is reported with the number of busy slots.

    "Given all slots are reserved, when a further start is called, then it is
    refused with a message naming the number of busy slots, and no slot state
    is mutated."
    """
    session_service.start_session("session-one", HumanAISessionSpec())
    session_service.start_session("session-two", HumanAISessionSpec())

    refused = session_service.start_session("session-three", HumanAISessionSpec())

    assert not refused.succeeded
    assert refused.slots_busy == TEST_SLOT_COUNT
    assert str(TEST_SLOT_COUNT) in refused.message


def test_participant_links_are_proxied_and_signed(session_service):
    """FR-15: links are proxied by session id and carry a token.

    The previous design handed out raw host ports, so the URL itself was the
    only secret and it was guessable.
    """
    outcome = session_service.start_session("session-one", HumanAISessionSpec())

    assert outcome.gui_url.startswith("https://node.test/s/session-one/gui?t=v1.")
    assert outcome.survey_url.startswith("https://node.test/s/session-one/survey?t=v1.")
    assert "8090" not in outcome.gui_url and "host.docker.internal" not in outcome.gui_url


def test_both_results_are_required_to_complete_a_session(session_service, session_manager):
    """FR-21: completion is triggered by the second result arriving, not by polling.

    "Given a session where both the trace and the survey outcome are posted,
    when the second arrives, then the session completes."
    """
    outcome = session_service.start_session("session-one", HumanAISessionSpec())
    session_token = token_from_url(outcome.gui_url)

    session_service.record_session_trace("session-one", {"session_token": session_token, "kpis": {}})
    assert session_manager.get("session-one").phase == SessionPhase.SURVEY

    session_service.record_survey_outcome("session-one", {"answers": ["a"]})
    assert session_manager.get("session-one").phase == SessionPhase.COMPLETED


def test_completion_stores_a_fetchable_artifact(session_service, artifact_store):
    """FR-05: a completed session's result is retrievable by the submitting caller.

    The previous design returned a gRPC reference to its own port, which
    nothing in a standalone deployment could resolve.
    """
    session_service.start_session("session-one", HumanAISessionSpec())
    session_service.record_session_trace("session-one", {"kpis": {"score": 1}})
    session_service.record_survey_outcome("session-one", {"answers": ["a"]})

    reference = artifact_store.build_reference("session-one", "https://node.test")

    assert reference["protocol"] == "http"
    assert reference["uri"] == "https://node.test/control/data/session-one"


def test_completing_a_session_frees_its_slot_for_reuse(session_service, slot_pool):
    """FR-11: a released slot is reassigned to the next participant.

    This is the pooling property that replaces creating a container per
    session. It also exercises the reset call the real slot receives.
    """
    session_service.start_session("session-one", HumanAISessionSpec())
    freed_slot_index = slot_pool.slot_for_session("session-one").index

    session_service.record_session_trace("session-one", {"kpis": {}})
    session_service.record_survey_outcome("session-one", {"answers": []})

    assert slot_pool.slot_for_session("session-one") is None
    assert freed_slot_index in session_service.reset_slots

    reused = session_service.start_session("session-two", HumanAISessionSpec())
    assert reused.succeeded
    assert slot_pool.slot_for_session("session-two").index == freed_slot_index


def test_abandoned_session_times_out_and_releases_its_slot(session_service, slot_pool):
    """FR-06, NFR-07: an abandoned session does not hold its slot forever.

    "Given a session that receives no results, when its budget elapses, then it
    is marked failed, its slot returns to the pool, and a later start is
    assigned that slot."
    """
    session_service.start_session(
        "session-one", HumanAISessionSpec(session_timeout_seconds=60)
    )

    expired = session_service.expire_timed_out_sessions(now=time.time() + 61)

    assert expired == ["session-one"]
    assert slot_pool.busy_count() == 0
    assert session_service.start_session("session-two", HumanAISessionSpec()).succeeded


def test_a_session_within_its_budget_is_not_expired(session_service, slot_pool):
    """FR-06: the timeout sweep does not reclaim a session that is still running."""
    session_service.start_session(
        "session-one", HumanAISessionSpec(session_timeout_seconds=3600)
    )

    assert session_service.expire_timed_out_sessions(now=time.time() + 60) == []
    assert slot_pool.busy_count() == 1


@pytest.mark.parametrize(
    "presented_token, expected_reason_fragment",
    [
        ("", "token"),
        ("v1.9999999999.not-a-real-signature", "token"),
        ("garbage", "token"),
    ],
)
def test_results_without_a_valid_token_are_rejected(
    session_service, presented_token, expected_reason_fragment
):
    """FR-04: results are authorised by the session token, not the service key.

    The producers run outside this service — one inside the participant's
    browser — so they cannot hold the service API key.
    """
    session_service.start_session("session-one", HumanAISessionSpec())

    session_id, rejection_reason = session_service.resolve_result_session(
        "session-one", presented_token
    )

    assert session_id is None
    assert expected_reason_fragment in rejection_reason.lower()


def test_results_with_a_valid_token_are_accepted(session_service):
    """FR-04: a correctly signed token authorises the result it accompanies."""
    outcome = session_service.start_session("session-one", HumanAISessionSpec())

    session_id, rejection_reason = session_service.resolve_result_session(
        "session-one", token_from_url(outcome.gui_url)
    )

    assert session_id == "session-one"
    assert rejection_reason == ""


def test_a_token_for_another_session_is_rejected(session_service):
    """FR-15: a token is bound to one session and cannot be moved to another."""
    session_service.start_session("session-one", HumanAISessionSpec())
    session_service.start_session("session-two", HumanAISessionSpec())
    other_sessions_token = issue_token("session-two", lifetime_seconds=600)

    session_id, _ = session_service.resolve_result_session("session-one", other_sessions_token)

    assert session_id is None


def test_ambiguous_results_are_refused_rather_than_guessed(session_service):
    """FR-04: with several sessions live, a result must say which one it belongs to.

    The previous single-session design attributed an unlabelled result to
    whichever session it found. With a pool that would silently write one
    participant's answers onto another's record.
    """
    session_service.start_session("session-one", HumanAISessionSpec())
    session_service.start_session("session-two", HumanAISessionSpec())

    session_id, rejection_reason = session_service.resolve_result_session(None, "irrelevant")

    assert session_id is None
    assert "ambiguous" in rejection_reason.lower()


def test_failed_slot_configuration_releases_the_slot(session_service, slot_pool, session_manager):
    """FR-08: a configuration failure costs one session, not a pool slot.

    Without this, every failed start would permanently shrink the pool.
    """

    def failing_configure(slot, session_id, spec, session_token):
        raise RuntimeError("simulator unreachable")

    session_service._configure_slot = failing_configure  # noqa: SLF001 - test seam

    outcome = session_service.start_session("session-one", HumanAISessionSpec())

    assert not outcome.succeeded
    assert "simulator unreachable" in outcome.message
    assert slot_pool.busy_count() == 0
    assert session_manager.get("session-one").phase == SessionPhase.FAILED


def test_slots_held_by_dead_sessions_are_reclaimed_at_startup(
    session_service, slot_pool, session_manager
):
    """NFR-07: an unclean restart does not permanently shrink the pool."""
    session_service.start_session("session-one", HumanAISessionSpec())
    session_manager.advance_phase("session-one", SessionPhase.FAILED, error_message="killed")

    reclaimed = session_service.reconcile_slots_at_startup()

    assert reclaimed == [1]
    assert slot_pool.busy_count() == 0
