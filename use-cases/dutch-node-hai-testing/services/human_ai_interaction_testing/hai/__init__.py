"""Human-AI Interaction Testing service.

The package is named ``hai`` rather than ``common`` so it can sit alongside the
shared ``common`` package the control plane now comes from, instead of shadowing
it. That shadowing is why this service previously carried a forked copy of the
control interface, and why the fork's missing API-key check went unnoticed.
"""

from .control_interface import create_app, run
from .session_operations import (
    collect_session_trace,
    collect_survey_outcome,
    session_handlers,
)

__all__ = [
    "create_app",
    "run",
    "session_handlers",
    "collect_session_trace",
    "collect_survey_outcome",
]
