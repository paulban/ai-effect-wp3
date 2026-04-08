"""Generic baseline algorithm for local benchmark tests.

This baseline intentionally emits a no-op style action and works with
environments exposing either action_space() or action_space.sample().
"""

from __future__ import annotations


class BaselineWrapper:
    def __init__(self, action_space):
        self._action_space = action_space

    def _default_action(self):
        action_space = self._action_space
        if action_space is None:
            raise RuntimeError("Environment does not expose an action space")

        if callable(action_space):
            return action_space()

        sample = getattr(action_space, "sample", None)
        if callable(sample):
            return sample()

        try:
            return action_space.get_do_nothing_action()
        except Exception as exc:
            raise RuntimeError("Unable to infer a default action") from exc

    def act(self, observation, reward=0.0, done=False):
        _ = (observation, reward, done)
        return self._default_action()


def build_agent(env, context):
    _ = context
    return BaselineWrapper(getattr(env, "action_space", None))
