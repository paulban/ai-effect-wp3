"""Artifact storage for services that return fetchable result references.

The orchestrator never transfers payloads: `/control/output` returns a small
DataReference and whoever wants the data resolves it themselves. For a service
that sits in a pipeline, that reference can point at a peer's gRPC endpoint,
because a downstream service is there to resolve it.

A *standalone* service has no downstream peer. Its caller is whoever submitted
the workflow, so its reference has to be something that caller can actually
fetch. This module provides that: artifacts are written to a directory and
served back over HTTP at `/control/data/{task_id}`, and `build_reference`
produces the matching DataReference.

The store is deliberately filesystem-backed rather than an object store. It
needs no extra container, no credentials, and survives a service restart when
pointed at a Docker volume. Swapping it for S3 or MinIO later changes only this
module — the DataReference the caller sees is identical either way.

Nothing prunes it. Every job writes one artifact under a key unique to its
workflow, so the volume grows without bound — a synthesized grid carries a full
node-link graph plus a pandapower network, so this is megabytes per run, not
bytes. `discard` exists for a retention policy that has not been written; until
it is, the ceiling is the volume's free space, and the failure when it arrives
lands on `store` at the *end* of a job, after the whole computation has been
spent. NFR-06 gives participant results a configurable retention period; the
batch services have no equivalent.

Spec coverage: FR-05, FR-25
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Default media type for artifacts stored without an explicit one. Most WP3
# services produce JSON results; binary producers pass their own type.
DEFAULT_MEDIA_TYPE = "application/json"

# Suffix of the sidecar file holding an artifact's media type and format. Kept
# beside the payload rather than in a central index so that storing an artifact
# is a two-file write with no shared state to lock.
METADATA_SUFFIX = ".meta.json"

# Suffix of the payload file itself.
PAYLOAD_SUFFIX = ".payload"

# Serialisation formats the orchestrator's DataReference accepts, keyed by the
# media type an artifact is served with. The orchestrator validates `format`
# against a closed set, so a reference is only usable if it names one of these.
WIRE_FORMAT_BY_MEDIA_TYPE = {
    "application/json": "json",
    "text/csv": "csv",
    "application/x-parquet": "parquet",
    "application/x-protobuf": "protobuf",
    "application/xml": "xml",
    "text/xml": "xml",
}

# Used when the media type is not one of the above. "binary" is always accepted
# and says the only true thing left: these are bytes.
DEFAULT_WIRE_FORMAT = "binary"

# Key under which the logical format travels in the reference's metadata.
DATA_FORMAT_METADATA_KEY = "data_format"


@dataclass(frozen=True)
class StoredArtifact:
    """An artifact retrieved from the store.

    Attributes:
        content: Raw bytes of the artifact, as written.
        media_type: MIME type to serve it with, e.g. "application/json".
        data_format: Logical format name carried in the DataReference, which
            describes *what* the payload is (e.g. "BenchmarkResult") rather
            than how it is encoded.
    """

    content: bytes
    media_type: str
    data_format: str


class FileArtifactStore:
    """Stores task artifacts on disk and serves them back by task id.

    One instance per service. Thread-safe for the access pattern these services
    have — one writer per task id, many concurrent readers — because each task
    owns its own files and writes are atomic (written to a temporary name, then
    renamed).

    Example:
        >>> store = FileArtifactStore(Path("/artifacts"))
        >>> store.store_json("task-1", {"score": 0.91}, data_format="BenchmarkResult")
        >>> store.load("task-1").data_format
        'BenchmarkResult'
    """

    def __init__(self, base_directory: Path | str) -> None:
        """
        Create a store rooted at a directory, creating it if necessary.

        Args:
            base_directory: Directory holding artifact payloads and metadata.
                Point this at a Docker volume so artifacts outlive a container
                restart.

        Raises:
            OSError: If the directory cannot be created.
        """
        self._base_directory = Path(base_directory)
        self._base_directory.mkdir(parents=True, exist_ok=True)

    def store(
        self,
        task_id: str,
        content: bytes,
        data_format: str,
        media_type: str = DEFAULT_MEDIA_TYPE,
    ) -> None:
        """
        Write an artifact and its metadata for later retrieval.

        The payload is written to a temporary file and renamed into place, so a
        reader polling for the artifact never observes a partial write.

        Args:
            task_id: Task or session identifier the artifact belongs to.
            content: Raw artifact bytes.
            data_format: Logical format recorded in the DataReference.
            media_type: MIME type used when serving the artifact.

        Raises:
            ValueError: If task_id is empty or contains a path separator, which
                would let a caller write outside the store directory.
        """
        self._validate_task_id(task_id)

        payload_path = self._payload_path(task_id)
        temporary_path = payload_path.with_suffix(payload_path.suffix + ".partial")
        temporary_path.write_bytes(content)
        temporary_path.replace(payload_path)

        self._metadata_path(task_id).write_text(
            json.dumps({"media_type": media_type, "data_format": data_format}),
            encoding="utf-8",
        )
        logger.info(
            "Stored artifact for task %s (%d bytes, %s)", task_id, len(content), data_format
        )

    def store_json(self, task_id: str, payload: dict, data_format: str) -> None:
        """
        Convenience wrapper storing a JSON-serialisable payload.

        Args:
            task_id: Task or session identifier.
            payload: Any JSON-serialisable object.
            data_format: Logical format recorded in the DataReference.

        Raises:
            TypeError: If the payload is not JSON-serialisable.
        """
        encoded = json.dumps(payload, indent=2).encode("utf-8")
        self.store(task_id, encoded, data_format=data_format, media_type="application/json")

    def load(self, task_id: str) -> StoredArtifact | None:
        """
        Read an artifact back.

        Args:
            task_id: Task or session identifier.

        Returns:
            The stored artifact, or None when no artifact exists for this id —
            which callers should surface as a 404 rather than an error.
        """
        self._validate_task_id(task_id)

        payload_path = self._payload_path(task_id)
        if not payload_path.exists():
            return None

        metadata = {"media_type": DEFAULT_MEDIA_TYPE, "data_format": "unknown"}
        metadata_path = self._metadata_path(task_id)
        if metadata_path.exists():
            try:
                metadata.update(json.loads(metadata_path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                # Metadata is an optimisation, not the artifact. Serving the
                # payload with defaults beats failing the fetch outright.
                logger.warning("Unreadable artifact metadata for task %s; using defaults", task_id)

        return StoredArtifact(
            content=payload_path.read_bytes(),
            media_type=metadata["media_type"],
            data_format=metadata["data_format"],
        )

    def build_reference(self, task_id: str, self_url: str) -> dict | None:
        """
        Build the DataReference pointing at this artifact's fetch URL.

        Args:
            task_id: Task or session identifier.
            self_url: Base URL at which this service is reachable by the party
                that will fetch the artifact, without a trailing slash.

        `format` names the *encoding*, not the payload. The orchestrator
        validates it against a closed set of serialisation formats, so a
        reference announcing itself as "GridData" or "BenchmarkResult" is
        rejected before the caller ever sees it — the workflow fails on the
        result it just successfully computed. The logical name the caller
        actually wants travels in `metadata`, which the orchestrator carries
        through to its task listing untouched.

        Returns:
            A DataReference mapping with protocol "http", or None when no
            artifact has been stored for this task.
        """
        artifact = self.load(task_id)
        if artifact is None:
            return None

        return {
            "protocol": "http",
            "uri": f"{self_url.rstrip('/')}/control/data/{task_id}",
            "format": WIRE_FORMAT_BY_MEDIA_TYPE.get(
                artifact.media_type, DEFAULT_WIRE_FORMAT
            ),
            "metadata": {DATA_FORMAT_METADATA_KEY: artifact.data_format},
        }

    def discard(self, task_id: str) -> None:
        """
        Delete an artifact and its metadata.

        Nothing calls this yet. It claimed to be used by retention enforcement,
        but no retention enforcement exists — see the note on unbounded growth
        in this module's docstring. Silently does nothing when the artifact is
        already gone.

        Args:
            task_id: Task or session identifier.
        """
        self._validate_task_id(task_id)
        self._payload_path(task_id).unlink(missing_ok=True)
        self._metadata_path(task_id).unlink(missing_ok=True)

    def discard_all(self) -> None:
        """Remove every artifact in the store.

        Intended for test teardown, though no test uses it today.
        """
        shutil.rmtree(self._base_directory, ignore_errors=True)
        self._base_directory.mkdir(parents=True, exist_ok=True)

    def _payload_path(self, task_id: str) -> Path:
        """Return the on-disk path of a task's payload file."""
        return self._base_directory / f"{task_id}{PAYLOAD_SUFFIX}"

    def _metadata_path(self, task_id: str) -> Path:
        """Return the on-disk path of a task's metadata sidecar."""
        return self._base_directory / f"{task_id}{METADATA_SUFFIX}"

    @staticmethod
    def _validate_task_id(task_id: str) -> None:
        """
        Reject task ids that are empty or could escape the store directory.

        Task ids reach this module from orchestrator requests and from URL
        path segments, so they are untrusted input. A id containing a path
        separator or a parent reference would otherwise let a caller read or
        write anywhere the process has access to.

        Args:
            task_id: Candidate identifier.

        Raises:
            ValueError: If the identifier is empty or not a single path-safe
                segment.
        """
        if not task_id:
            raise ValueError("task_id must not be empty")

        if "/" in task_id or "\\" in task_id or task_id in (".", ".."):
            raise ValueError(f"task_id must be a single path segment, got {task_id!r}")
