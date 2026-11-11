"""
Agent wrapper — serves a benchmarking-style algorithm as an InteractiveAI agent.

InteractiveAI's recommendation service (``cabrecommendation``) asks external
agents for recommendations over HTTP: it POSTs ``{event, context, ...}`` to the
URL in ``RL_AGENT_API_URL`` and expects a JSON list of recommendations back. The
setup script points that URL at this service once and for all, so switching the
algorithm per session needs no container restart: the session installs its
agent here, and the endpoint answers with it.

Per request, the wrapper:

1. reads the grid2op observation the simulator posted as context
   (``context.data.observation``, the output of ``obs.to_json()``);
2. loads it into an observation of its own environment, built exactly as the
   simulator builds its own so both share action and observation spaces;
3. calls ``agent.act(observation)`` — the benchmarking contract, one argument;
4. returns one recommendation whose ``actions[0]`` is ``action.to_json()``. The
   frontend applies ``actions[0]`` and the simulator rebuilds the action from it
   with ``action_space().from_json``.

A failure here never fails the session: the wrapper answers ``[]``, which
InteractiveAI shows as "no recommendation", and records a warning.

Limitation: an observation rebuilt from JSON is not attached to the simulator's
live environment, so algorithms that call ``observation.simulate`` get the
wrapper environment's forecast state, not the participant's.

Spec coverage: FR-12, FR-16, FR-17, FR-18, FR-19, FR-19a, section 5.5
"""

from __future__ import annotations

import importlib.util
import json
import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Optional

from fastapi import APIRouter, Body, HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from session_config import ScenarioSelection, SessionConfig
from session_warnings import SessionWarnings
from settings import (
    INTERACTIVEAI_USE_CASE,
    MAXIMUM_RECOMMENDATION_DESCRIPTION_LENGTH,
    SIMULATOR_FORECAST_HORIZONS_MINUTES,
    HAISettings,
)

logger = logging.getLogger(__name__)

# The frontend's recommendation type declares agent_type as the literal 'IA'
# (frontend/src/types/services.ts).
RECOMMENDATION_AGENT_TYPE = "IA"
DO_NOTHING_TITLE = "Do nothing"
ACTION_TITLE = "Recommended action"

# build_agent's ``context["use_case"]`` (section 5.5), so an algorithm shared
# with benchmarking can tell where it runs.
BUILD_AGENT_USE_CASE = "hai_session"

_agent_bearer = HTTPBearer(auto_error=False)


# --- Environment and algorithm --------------------------------------------------


def _find_chronic_index(environment: Any, scenario_name: str) -> int:
    """
    Return the index of a chronic folder in the environment, as the simulator does.

    Mirrors ``search_chronic_num_from_name`` in the simulator's utils.

    Raises:
        ValueError: If no chronic folder has that name.
    """
    chronic_names = [Path(subpath).name for subpath in environment.chronics_handler.real_data.subpaths]
    if scenario_name not in chronic_names:
        raise ValueError(f"Chronic {scenario_name!r} not found in the environment")
    return chronic_names.index(scenario_name)


def build_simulator_environment(
    environment_directory: Path,
    scenario: ScenarioSelection,
    backend_factory: Optional[Callable[[], Any]] = None,
) -> Any:
    """
    Build a grid2op environment the way the PowerGrid simulator builds its own (FR-17).

    The arguments are copied from ``Simulator.initialize_simulation`` at
    InteractiveAI 600a1ce: LightSim backend, ``FromHandlers`` chronics with CSV
    handlers for the injections and perfect-forecast handlers for the forecasts.
    A plain ``grid2op.make(path)`` would pick a different chronics class, and
    the simulator's observations would not load into it.

    Args:
        environment_directory: The grid2op environment directory.
        scenario: Selects the chronic and seed.
        backend_factory: Builds the backend; LightSimBackend when omitted.

    Returns:
        The environment, seeded, set to the chronic and reset.

    Raises:
        ValueError: If the chronic is not in the environment.
    """
    # Imported here so the rest of the service, and its tests, load without grid2op.
    import grid2op
    from grid2op.Chronics import FromHandlers
    from grid2op.Chronics.handlers import CSVHandler, PerfectForecastHandler

    if backend_factory is None:
        from lightsim2grid import LightSimBackend

        backend_factory = LightSimBackend

    environment = grid2op.make(
        str(environment_directory),
        backend=backend_factory(),
        data_feeding_kwargs={
            "gridvalueClass": FromHandlers,
            "gen_p_handler": CSVHandler("prod_p"),
            "load_p_handler": CSVHandler("load_p"),
            "gen_v_handler": CSVHandler("prod_v"),
            "load_q_handler": CSVHandler("load_q"),
            "h_forecast": list(SIMULATOR_FORECAST_HORIZONS_MINUTES),
            "gen_p_for_handler": PerfectForecastHandler("prod_p_forecasted"),
            "load_p_for_handler": PerfectForecastHandler("load_p_forecasted"),
            "load_q_for_handler": PerfectForecastHandler("load_q_forecasted"),
        },
    )
    environment.seed(scenario.env_seed)
    environment.set_id(_find_chronic_index(environment, scenario.scenario_name))
    environment.reset()
    return environment


def load_algorithm_module(algorithm_path: Path) -> ModuleType:
    """
    Import an algorithm file under a unique module name.

    A unique name per load means two sessions using files of the same name, or
    the same file edited in between, never share a cached module.

    Args:
        algorithm_path: The ``.py`` file.

    Returns:
        The imported module.

    Raises:
        ImportError: If the file cannot be loaded.
        AttributeError: If it defines no ``build_agent``.
    """
    module_name = f"hai_algorithm_{algorithm_path.stem}_{uuid.uuid4().hex[:8]}"
    module_spec = importlib.util.spec_from_file_location(module_name, algorithm_path)
    if module_spec is None or module_spec.loader is None:
        raise ImportError(f"Cannot load algorithm file {algorithm_path}")
    algorithm_module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(algorithm_module)
    if not callable(getattr(algorithm_module, "build_agent", None)):
        raise AttributeError(f"{algorithm_path.name} defines no build_agent(env, context)")
    return algorithm_module


def build_agent_context(config: SessionConfig) -> dict[str, Any]:
    """
    Return the ``context`` argument handed to ``build_agent`` (section 5.5).

    Args:
        config: The validated session request.

    Returns:
        The context mapping.
    """
    return {
        "use_case": BUILD_AGENT_USE_CASE,
        "scenario": config.to_result_dict()["scenario"],
        "session_duration_seconds": config.session_duration_seconds,
        "scenario_directory": str(config.scenario_directory.resolve()),
    }


# --- Active agent -----------------------------------------------------------------


@dataclass
class ActiveAgent:
    """
    The algorithm currently answering recommendation requests.

    Attributes:
        job_id: Session the agent belongs to.
        environment: Wrapper environment, sharing the simulator's spaces.
        agent: Object returned by ``build_agent``, with ``act(observation)``.
        session_warnings: Where request failures are recorded.
        act_executor: Runs ``agent.act`` so a slow call can be timed out (FR-19a).
            One worker, so calls never overlap on a non-thread-safe agent.
    """

    job_id: str
    environment: Any
    agent: Any
    session_warnings: SessionWarnings
    act_executor: ThreadPoolExecutor = field(
        default_factory=lambda: ThreadPoolExecutor(max_workers=1, thread_name_prefix="hai-act")
    )


def activate_algorithm(
    config: SessionConfig,
    job_id: str,
    session_warnings: SessionWarnings,
    backend_factory: Optional[Callable[[], Any]] = None,
) -> ActiveAgent:
    """
    Build the wrapper environment and the session's agent (FR-12).

    Args:
        config: The validated session request.
        job_id: The session's job id.
        session_warnings: The session's warning log.
        backend_factory: Overrides the grid2op backend, for tests.

    Returns:
        The agent, ready to install in an ``AgentRegistry``.

    Raises:
        Exception: Whatever the environment build, the import or ``build_agent`` raises.
    """
    environment = build_simulator_environment(config.scenario_directory, config.scenario, backend_factory)
    algorithm_module = load_algorithm_module(config.algorithm_path)
    agent = algorithm_module.build_agent(environment, build_agent_context(config))
    if not callable(getattr(agent, "act", None)):
        raise TypeError(f"build_agent in {config.algorithm} returned an object without act(observation)")
    return ActiveAgent(job_id=job_id, environment=environment, agent=agent, session_warnings=session_warnings)


class AgentRegistry:
    """
    Holds the one active agent, shared between the session job and request threads.

    Example:
        >>> registry = AgentRegistry()
        >>> registry.current() is None
        True
    """

    def __init__(self) -> None:
        self._active_agent: ActiveAgent | None = None
        self._lock = threading.Lock()

    def activate(self, active_agent: ActiveAgent) -> None:
        """
        Install the session's agent, replacing any previous one.

        Args:
            active_agent: The agent to answer requests with from now on.
        """
        with self._lock:
            previous_agent = self._active_agent
            self._active_agent = active_agent
        if previous_agent is not None:
            previous_agent.act_executor.shutdown(wait=False, cancel_futures=True)

    def deactivate(self, job_id: str | None = None) -> None:
        """
        Remove the active agent, so requests are answered with ``[]``.

        Args:
            job_id: Only deactivate if the active agent belongs to this session.
                None deactivates whatever is active.
        """
        with self._lock:
            active_agent = self._active_agent
            if active_agent is None or (job_id is not None and active_agent.job_id != job_id):
                return
            self._active_agent = None
        # Not waiting: an act() stuck past its timeout must not hold up the session.
        active_agent.act_executor.shutdown(wait=False, cancel_futures=True)

    def current(self) -> ActiveAgent | None:
        """Return the active agent, or None."""
        with self._lock:
            return self._active_agent


# --- Recommendation ---------------------------------------------------------------


def extract_observation_payload(request_body: Any) -> dict[str, Any]:
    """
    Return the serialised observation from a recommendation request.

    The simulator posts its context as ``{"date", "data": {"observation",
    "topology"}, "use_case"}``, and cabrecommendation forwards it as ``context``.

    Args:
        request_body: The decoded POST body.

    Returns:
        The ``obs.to_json()`` mapping.

    Raises:
        ValueError: Naming the top-level keys present, if the observation is missing.
    """
    try:
        observation_payload = request_body["context"]["data"]["observation"]
    except (KeyError, TypeError) as missing_key:
        present_keys = sorted(request_body) if isinstance(request_body, dict) else type(request_body).__name__
        raise ValueError(
            f"No context.data.observation in the recommendation request (top-level keys: {present_keys})"
        ) from missing_key
    if isinstance(observation_payload, str):
        observation_payload = json.loads(observation_payload)
    if not isinstance(observation_payload, dict):
        raise ValueError("context.data.observation is not a JSON object")
    return observation_payload


def rebuild_observation(environment: Any, observation_payload: dict[str, Any]) -> Any:
    """
    Load a simulator observation into an observation of the wrapper environment.

    Starts from a copy of the environment's current observation, which has the
    right class and shapes, and overwrites every serialised attribute with
    ``from_json``. (``observation_space()`` is not a constructor in grid2op 1.9.)

    Args:
        environment: The wrapper environment.
        observation_payload: Output of the simulator's ``obs.to_json()``.

    Returns:
        A grid2op observation equal to the simulator's.
    """
    observation = environment.get_obs().copy()
    observation.from_json(observation_payload)
    return observation


def action_to_recommendation(action: Any, environment: Any) -> dict[str, Any]:
    """
    Turn a grid2op action into the recommendation format InteractiveAI expects (FR-18).

    Args:
        action: The algorithm's action.
        environment: The wrapper environment, for the do-nothing comparison.

    Returns:
        One recommendation mapping.
    """
    # Same comparison the simulator uses to detect a do-nothing action.
    is_do_nothing = action == environment.action_space({})
    return {
        "title": DO_NOTHING_TITLE if is_do_nothing else ACTION_TITLE,
        "description": str(action)[:MAXIMUM_RECOMMENDATION_DESCRIPTION_LENGTH],
        "use_case": INTERACTIVEAI_USE_CASE,
        "agent_type": RECOMMENDATION_AGENT_TYPE,
        "actions": [action.to_json()],
        "kpis": {},
    }


def recommend(request_body: Any, active_agent: ActiveAgent | None, act_timeout_seconds: float) -> list[dict]:
    """
    Answer one recommendation request; never raises (FR-18, FR-19, FR-19a).

    Args:
        request_body: The decoded POST body.
        active_agent: The session's agent, or None outside a session.
        act_timeout_seconds: Longest ``agent.act`` may take.

    Returns:
        One recommendation, or ``[]`` when there is no session or anything fails.
    """
    if active_agent is None:
        return []

    try:
        observation = rebuild_observation(active_agent.environment, extract_observation_payload(request_body))
        act_future = active_agent.act_executor.submit(active_agent.agent.act, observation)
        action = act_future.result(timeout=act_timeout_seconds)
        return [action_to_recommendation(action, active_agent.environment)]
    except FutureTimeoutError:
        message = f"agent.act did not return within {act_timeout_seconds:g}s; answered with no recommendation"
    except Exception as recommendation_error:  # noqa: BLE001 - FR-19: never fail the request
        message = f"recommendation failed: {type(recommendation_error).__name__}: {recommendation_error}"

    logger.warning("Session %s: %s", active_agent.job_id, message)
    active_agent.session_warnings.add(message)
    return []


# --- HTTP ---------------------------------------------------------------------------


def create_agent_router(registry: AgentRegistry, settings_provider: Callable[[], HAISettings]) -> APIRouter:
    """
    Build the router cabrecommendation calls as its agent (FR-16).

    Args:
        registry: Supplies the active agent.
        settings_provider: Returns the current settings (token, act timeout).

    Returns:
        A router with ``POST /api/v1/recommendation`` and ``GET /api/v1/health``.
    """
    router = APIRouter(prefix="/api/v1", tags=["agent"])

    def require_agent_token(credentials: Optional[HTTPAuthorizationCredentials]) -> None:
        """Reject the request unless it carries HAI_AGENT_TOKEN, when one is set."""
        expected_token = settings_provider().agent_token
        if expected_token and (credentials is None or credentials.credentials != expected_token):
            raise HTTPException(status_code=401, detail="Unauthorized")

    @router.post("/recommendation")
    def post_recommendation(
        request_body: Any = Body(default=None),
        credentials: Optional[HTTPAuthorizationCredentials] = Security(_agent_bearer),
    ) -> list[dict]:
        """Answer cabrecommendation with the active algorithm's recommendation."""
        require_agent_token(credentials)
        return recommend(request_body, registry.current(), settings_provider().agent_act_timeout_seconds)

    @router.get("/health")
    def get_health() -> dict:
        """Report liveness and which session, if any, is answering."""
        active_agent = registry.current()
        return {"status": "ok", "active_session": active_agent.job_id if active_agent else None}

    return router
