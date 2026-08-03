"""Human-AI Interaction Testing service entrypoint."""

from common import collect_session_trace, session_handlers, run, start_grpc_server

if __name__ == "__main__":
    grpc_server = start_grpc_server()
    run(
        session_handlers,
        "Human-AI Interaction Testing Service",
        collect_session_trace=collect_session_trace,
    )
