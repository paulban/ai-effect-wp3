"""Grid2Op benchmark service.

Named ``benchmark`` rather than ``common`` so it sits alongside the shared
``common`` package the control plane comes from, instead of shadowing it.
"""

from .benchmark_operations import benchmark_handlers, execute_RunBenchmark

__all__ = ["benchmark_handlers", "execute_RunBenchmark"]
