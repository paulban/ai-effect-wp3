"""Human-AI Interaction Testing service entrypoint.

Reclaims any slots left reserved by a previous process before serving, so an
unclean restart does not permanently shrink the pool, then starts the HTTP
control plane. There is no gRPC server and no background polling thread: the
service is idle between requests, and results arrive by being posted to it.
"""

import logging

from hai import collect_session_trace, collect_survey_outcome, run, session_handlers
from hai.session_service import get_session_service

logger = logging.getLogger(__name__)


def main() -> None:
    """Reconcile pool state, then serve the control plane."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    )

    reclaimed_slot_indices = get_session_service().reconcile_slots_at_startup()
    if reclaimed_slot_indices:
        logger.info("Reclaimed slots from a previous run: %s", reclaimed_slot_indices)

    run(
        session_handlers,
        collect_session_trace=collect_session_trace,
        collect_survey_outcome=collect_survey_outcome,
    )


if __name__ == "__main__":
    main()
