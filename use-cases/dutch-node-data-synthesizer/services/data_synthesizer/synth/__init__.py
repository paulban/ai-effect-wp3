"""Synthetic power grid service.

Named ``synth`` rather than ``common`` so it sits alongside the shared
``common`` package the control plane comes from, instead of shadowing it.
"""

from .synth_operations import execute_ConfigureAndSynthesize, synth_handlers

__all__ = ["synth_handlers", "execute_ConfigureAndSynthesize"]
