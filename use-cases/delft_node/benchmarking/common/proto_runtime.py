from __future__ import annotations

import importlib
import sys
from pathlib import Path


def ensure_generated(*proto_files: str) -> Path:
    """Compile shared proto files into a local generated module directory."""
    common_dir = Path(__file__).resolve().parent
    service_root = common_dir.parent
    shared_proto_dir = service_root / "shared" / "proto"
    generated_dir = common_dir / "_generated"
    generated_dir.mkdir(parents=True, exist_ok=True)

    if str(generated_dir) not in sys.path:
        sys.path.insert(0, str(generated_dir))

    missing = []
    for proto in proto_files:
        stem = Path(proto).stem
        if (
            not (generated_dir / f"{stem}_pb2.py").exists()
            or not (generated_dir / f"{stem}_pb2_grpc.py").exists()
        ):
            missing.append(proto)

    if not missing:
        return generated_dir

    try:
        from grpc_tools import protoc
    except Exception as exc:
        raise RuntimeError(
            "grpc_tools is required to compile protobuf modules. "
            "Install grpcio-tools in this service environment."
        ) from exc

    args = [
        "grpc_tools.protoc",
        f"-I{shared_proto_dir}",
        f"--python_out={generated_dir}",
        f"--grpc_python_out={generated_dir}",
    ] + [str(shared_proto_dir / proto) for proto in proto_files]

    rc = protoc.main(args)
    if rc != 0:
        raise RuntimeError(f"protoc failed with exit code {rc}")

    importlib.invalidate_caches()
    return generated_dir
