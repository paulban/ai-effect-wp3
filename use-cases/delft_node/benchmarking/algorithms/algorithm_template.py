"""L2RPN-style template for Delft benchmark algorithms.

Implement build_agent(env, context) and the agent act(observation) method.
The benchmark runner imports this file dynamically.
"""

from __future__ import annotations


class TemplateAgent:
    """Simple safe baseline agent for validation runs."""

    def __init__(self, action_space):
        self._action_space = action_space

    def act(self, observation):
        # Baseline no-op action.
        return self._action_space()


def build_agent(env, context):
    """Return an agent instance with act(observation) -> action.

    Args:
        env: Grid2Op environment.
        context: Dict with benchmark, grid_topology, and time_series inputs.
    """
    _ = context
    return TemplateAgent(env.action_space)
