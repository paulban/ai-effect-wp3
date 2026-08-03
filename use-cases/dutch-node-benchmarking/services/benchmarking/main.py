"""Dutch Node benchmarking service entrypoint."""

from common import benchmark_handlers, run, start_grpc_server

if __name__ == "__main__":
    grpc_server = start_grpc_server()
    run(benchmark_handlers, "Dutch Grid2Op Benchmark")
