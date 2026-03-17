"""Delft Node benchmarking service entrypoint."""

from common import benchmark_handlers, run


if __name__ == "__main__":
    run(benchmark_handlers, "Delft Grid2Op Benchmark")
