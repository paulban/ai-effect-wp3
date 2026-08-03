from __future__ import annotations

import importlib
import sys
from pathlib import Path


def ensure_generated(*proto_files: str) -> Path:
    """Compile service-local proto files into a local generated module directory."""
    common_dir = Path(__file__).resolve().parent
    service_root = common_dir.parent
    benchmark_proto_dir = service_root / "proto"
    # Vendored copy of the data synthesizer's proto. It used to be resolved as a
    # sibling service directory, which stopped working when the synthesizer moved
    # into its own use case with its own Docker build context.
    synth_proto_dir = service_root / "external_proto"
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

    def resolve_proto(proto: str) -> Path:
        direct = Path(proto)
        if direct.is_absolute() and direct.exists():
            return direct
        for base in (benchmark_proto_dir, synth_proto_dir):
            candidate = base / proto
            if candidate.exists():
                return candidate
        raise FileNotFoundError(f"Proto file not found in known proto dirs: {proto}")

    args = [
        "grpc_tools.protoc",
        f"-I{benchmark_proto_dir}",
        f"-I{synth_proto_dir}",
        f"--python_out={generated_dir}",
        f"--grpc_python_out={generated_dir}",
    ] + [str(resolve_proto(proto)) for proto in proto_files]

    rc = protoc.main(args)
    if rc != 0:
        raise RuntimeError(f"protoc failed with exit code {rc}")

    importlib.invalidate_caches()
    return generated_dir
