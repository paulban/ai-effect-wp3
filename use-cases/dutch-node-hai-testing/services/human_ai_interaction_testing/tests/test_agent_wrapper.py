"""Agent wrapper: endpoint behaviour and grid2op round trips (spec FR-12, FR-16 to FR-19a)."""

from __future__ import annotations

import dataclasses
import json
import time

import pytest
from conftest import SIMULATOR_TEST_CHRONIC, SIMULATOR_TEST_ENVIRONMENT, valid_payload
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_wrapper import ActiveAgent, AgentRegistry, activate_algorithm, create_agent_router
from session_config import parse_session_config
from session_warnings import SessionWarnings

REQUEST_WITHOUT_OBSERVATION = {"event": {}, "context": {"date": "x", "data": {}}}


class StubObservation:
    """Observation stand-in: accepts any JSON."""

    def copy(self):
        return StubObservation()

    def from_json(self, payload):
        self.payload = payload


class StubEnvironment:
    """Just enough environment for the wrapper's non-grid2op paths."""

    def get_obs(self):
        return StubObservation()


class SlowAgent:
    def act(self, observation):
        time.sleep(1.0)


class RaisingAgent:
    def act(self, observation):
        raise RuntimeError("act exploded")


def request_with_observation(observation_payload) -> dict:
    """A recommendation request shaped as cabrecommendation forwards it."""
    return {
        "event": {"title": "Overload"},
        "context": {
            "date": "2026-11-11 10:00:00",
            "data": {"observation": observation_payload, "topology": ""},
            "use_case": "PowerGrid",
        },
    }


def make_client(registry: AgentRegistry, settings) -> TestClient:
    application = FastAPI()
    application.include_router(create_agent_router(registry, lambda: settings))
    return TestClient(application)


def install_stub_agent(registry: AgentRegistry, agent) -> SessionWarnings:
    session_warnings = SessionWarnings()
    registry.activate(
        ActiveAgent(job_id="job-1", environment=StubEnvironment(), agent=agent, session_warnings=session_warnings)
    )
    return session_warnings


def test_no_active_session_answers_empty_list(hai_settings):
    """FR-19: with no active session the endpoint returns ``200 []``."""
    client = make_client(AgentRegistry(), hai_settings)
    response = client.post("/api/v1/recommendation", json=request_with_observation({}))
    assert response.status_code == 200
    assert response.json() == []


def test_health_reports_active_session(hai_settings):
    """FR-16: health is open and names the active session."""
    registry = AgentRegistry()
    client = make_client(registry, hai_settings)
    assert client.get("/api/v1/health").json() == {"status": "ok", "active_session": None}

    install_stub_agent(registry, RaisingAgent())
    assert client.get("/api/v1/health").json() == {"status": "ok", "active_session": "job-1"}


def test_wrong_agent_token_is_rejected(hai_settings):
    """Agent: token (9.2) — with HAI_AGENT_TOKEN set, a wrong bearer gets 401."""
    settings = dataclasses.replace(hai_settings, agent_token="secret")
    client = make_client(AgentRegistry(), settings)

    assert client.post("/api/v1/recommendation", json={}, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.post("/api/v1/recommendation", json={}).status_code == 401
    assert client.post("/api/v1/recommendation", json={}, headers={"Authorization": "Bearer secret"}).status_code == 200


def test_missing_observation_answers_empty_and_warns(hai_settings):
    """Agent: bad body (9.2) — no ``context.data.observation`` gives ``[]`` and a warning."""
    registry = AgentRegistry()
    session_warnings = install_stub_agent(registry, RaisingAgent())

    response = make_client(registry, hai_settings).post("/api/v1/recommendation", json=REQUEST_WITHOUT_OBSERVATION)

    assert response.json() == []
    assert len(session_warnings.as_list()) == 1
    assert "context.data.observation" in session_warnings.as_list()[0]


def test_raising_agent_answers_empty_and_warns(hai_settings):
    """FR-19: an exception in ``agent.act`` gives ``[]`` and a warning, never an error."""
    registry = AgentRegistry()
    session_warnings = install_stub_agent(registry, RaisingAgent())

    response = make_client(registry, hai_settings).post("/api/v1/recommendation", json=request_with_observation({}))

    assert response.status_code == 200
    assert response.json() == []
    assert "act exploded" in session_warnings.as_list()[0]


def test_slow_agent_times_out(hai_settings):
    """Agent: slow act (9.2) — past the act timeout, ``[]`` and a warning."""
    settings = dataclasses.replace(hai_settings, agent_act_timeout_seconds=0.1)
    registry = AgentRegistry()
    session_warnings = install_stub_agent(registry, SlowAgent())

    response = make_client(registry, settings).post("/api/v1/recommendation", json=request_with_observation({}))

    assert response.json() == []
    assert "did not return within" in session_warnings.as_list()[0]
    registry.deactivate()


def test_warnings_are_capped():
    """FR-19: at most 50 warnings are kept, plus a count of the dropped ones."""
    session_warnings = SessionWarnings()
    for index in range(53):
        session_warnings.add(f"warning {index}")
    recorded = session_warnings.as_list()
    assert len(recorded) == 51
    assert recorded[-1] == "3 further warning(s) not recorded"


def test_deactivate_ignores_other_session():
    """Deactivating for another job leaves the active agent in place."""
    registry = AgentRegistry()
    install_stub_agent(registry, RaisingAgent())
    registry.deactivate("another-job")
    assert registry.current() is not None
    registry.deactivate("job-1")
    assert registry.current() is None


# --- grid2op round trips -------------------------------------------------------------

DISCONNECTING_ALGORITHM_SOURCE = '''
class DisconnectLineZero:
    def __init__(self, action_space):
        self.action_space = action_space

    def act(self, observation):
        return self.action_space({"set_line_status": [(0, -1)]})


def build_agent(env, context):
    assert context["use_case"] == "hai_session"
    return DisconnectLineZero(env.action_space)
'''


@pytest.fixture(scope="module")
def simulator_environment_available():
    pytest.importorskip("grid2op")
    pytest.importorskip("lightsim2grid")
    if not SIMULATOR_TEST_ENVIRONMENT.is_dir():
        pytest.skip("InteractiveAI submodule not checked out")


@pytest.fixture
def grid2op_settings(simulator_environment_available, hai_settings, node_directories):
    """Settings whose scenarios folder is the simulator's own test environment."""
    (node_directories["algorithms"] / "disconnecting_algorithm.py").write_text(
        DISCONNECTING_ALGORITHM_SOURCE, encoding="utf-8"
    )
    return dataclasses.replace(hai_settings, scenario_directory=SIMULATOR_TEST_ENVIRONMENT.parent)


def activate(settings, algorithm_name):
    """Validate a request for the simulator test environment and activate its algorithm."""
    payload = valid_payload(
        algorithm=algorithm_name,
        scenario={"env_name": SIMULATOR_TEST_ENVIRONMENT.name, "scenario_name": SIMULATOR_TEST_CHRONIC},
    )
    config = parse_session_config(payload, settings.algorithm_directory, settings.scenario_directory)
    return activate_algorithm(config, "job-grid2op", SessionWarnings())


def simulator_observation_json(environment) -> dict:
    """An observation as the simulator posts it: ``obs.to_json()`` through a JSON round trip."""
    observation, *_ = environment.step(environment.action_space({}))
    return json.loads(json.dumps(observation.to_json()))


def reload_as_simulator_does(environment, action_json):
    """Rebuild an applied action exactly like the simulator's ``expand_act_from_cab``."""
    action = environment.action_space()
    action.from_json(action_json)
    rebuilt_action = environment.action_space()
    rebuilt_action.from_vect(action.to_vect())
    return rebuilt_action


def test_template_algorithm_round_trip(grid2op_settings):
    """FR-12/16/18: the template answers a real observation with one "Do nothing" whose
    ``actions[0]`` the simulator can reload."""
    # Given
    registry = AgentRegistry()
    active_agent = activate(grid2op_settings, "algorithm_template.py")
    registry.activate(active_agent)
    observation_json = simulator_observation_json(active_agent.environment)

    # When
    response = make_client(registry, grid2op_settings).post(
        "/api/v1/recommendation", json=request_with_observation(observation_json)
    )

    # Then
    recommendations = response.json()
    assert len(recommendations) == 1
    recommendation = recommendations[0]
    assert recommendation["title"] == "Do nothing"
    assert recommendation["use_case"] == "PowerGrid"
    assert recommendation["agent_type"] == "IA"
    environment = active_agent.environment
    assert reload_as_simulator_does(environment, recommendation["actions"][0]) == environment.action_space({})
    assert active_agent.session_warnings.as_list() == []
    registry.deactivate()


def test_acting_algorithm_round_trip(grid2op_settings):
    """FR-17/18: a non-trivial action survives the JSON round trip to the simulator."""
    registry = AgentRegistry()
    active_agent = activate(grid2op_settings, "disconnecting_algorithm.py")
    registry.activate(active_agent)
    environment = active_agent.environment

    response = make_client(registry, grid2op_settings).post(
        "/api/v1/recommendation", json=request_with_observation(simulator_observation_json(environment))
    )

    recommendation = response.json()[0]
    assert recommendation["title"] == "Recommended action"
    expected_action = environment.action_space({"set_line_status": [(0, -1)]})
    assert reload_as_simulator_does(environment, recommendation["actions"][0]) == expected_action
    registry.deactivate()


def test_observation_round_trip_is_exact(grid2op_settings):
    """FR-18: the rebuilt observation equals the simulator's."""
    import numpy

    from agent_wrapper import rebuild_observation

    active_agent = activate(grid2op_settings, "algorithm_template.py")
    environment = active_agent.environment
    observation, *_ = environment.step(environment.action_space({}))

    rebuilt = rebuild_observation(environment, json.loads(json.dumps(observation.to_json())))

    assert numpy.allclose(rebuilt.rho, observation.rho)
    assert rebuilt.current_step == observation.current_step
    assert numpy.array_equal(rebuilt.topo_vect, observation.topo_vect)


def test_build_agent_error_propagates(grid2op_settings):
    """FR-12: an exception in ``build_agent`` reaches the caller (the session fails)."""
    with pytest.raises(RuntimeError, match="build_agent exploded"):
        activate(grid2op_settings, "raising_algorithm.py")
