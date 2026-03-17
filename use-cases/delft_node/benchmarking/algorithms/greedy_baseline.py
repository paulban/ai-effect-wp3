"""Grid2Op baseline algorithm for local benchmark tests.

This baseline tries a built-in Grid2Op heuristic agent first, and falls back
on DoNothingAgent if the preferred heuristic is unavailable.
"""

from __future__ import annotations


class BaselineWrapper:
    def __init__(self, env):
        self._agent = None
        self._init_agent(env)

    def _init_agent(self, env) -> None:
        try:
            from grid2op.Agent import RecoPowerlineAgent

            self._agent = RecoPowerlineAgent(env.action_space)
            return
        except Exception:
            pass

        try:
            from grid2op.Agent import DoNothingAgent

            self._agent = DoNothingAgent(env.action_space)
            return
        except Exception as exc:
            raise RuntimeError("Unable to create a Grid2Op baseline agent.") from exc

    def act(self, observation, reward=0.0, done=False):
        # Keep compatibility with multiple Grid2Op agent signatures.
        try:
            return self._agent.act(observation, reward, done)
        except TypeError:
            try:
                return self._agent.act(observation, reward)
            except TypeError:
                return self._agent.act(observation)


def build_agent(env, context):
    _ = context
    return BaselineWrapper(env)
