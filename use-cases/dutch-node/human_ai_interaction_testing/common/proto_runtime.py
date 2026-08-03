"""Proto compilation helper for the Human-AI Interaction Testing service.

Compiles the service proto file into Python gRPC modules on first use and
caches them in common/_generated/. Adapted from the benchmarking service's
proto_runtime.py to resolve the single service proto directory.

Spec coverage: supporting FR-04 runtime
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path


def ensure_generated(*proto_files: str) -> Path:
    """Compile service proto files into Python gRPC modules if not already done.

    Modules are placed in common/_generated/ and added to sys.path so they can
    be imported directly (e.g. `import human_ai_interaction_testing_pb2`).

    Args:
        *proto_files: Proto filenames (basename only) to compile, e.g.
                      ``"human_ai_interaction_testing.proto"``.

    Returns:
        The absolute path to the _generated directory.

    Raises:
        FileNotFoundError: If a proto file cannot be found in the proto directory.
        RuntimeError: If grpc_tools is not installed or protoc compilation fails.
    """
    common_dir = Path(__file__).resolve().parent
    service_root = common_dir.parent
    proto_dir = service_root / "proto"
    generated_dir = common_dir / "_generated"
    generated_dir.mkdir(parents=True, exist_ok=True)

    if str(generated_dir) not in sys.path:
        sys.path.insert(0, str(generated_dir))

    missing = [
        proto
        for proto in proto_files
        if not (generated_dir / f"{Path(proto).stem}_pb2.py").exists()
        or not (generated_dir / f"{Path(proto).stem}_pb2_grpc.py").exists()
    ]

    if not missing:
        return generated_dir

    try:
        from grpc_tools import protoc
    except Exception as exc:
        raise RuntimeError(
            "grpc_tools is required to compile protobuf modules. "
            "Install grpcio-tools in this service environment."
        ) from exc

    def resolve_proto(proto: str) -> Path:
        candidate = proto_dir / proto
        if candidate.exists():
            return candidate
        raise FileNotFoundError(
            f"Proto file not found in {proto_dir}: {proto}"
        )

    args = [
        "grpc_tools.protoc",
        f"-I{proto_dir}",
        f"--python_out={generated_dir}",
        f"--grpc_python_out={generated_dir}",
    ] + [str(resolve_proto(proto)) for proto in missing]

    return_code = protoc.main(args)
    if return_code != 0:
        raise RuntimeError(f"protoc failed with exit code {return_code}")

    importlib.invalidate_caches()
    return generated_dir
