"""Data models for orchestrator nodes and operations."""

from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any


@dataclass
class OperationSignature:
    """Represents a gRPC operation signature with full message type details."""
    operation_name: str
    input_message_name: Optional[str] = None
    output_message_name: Optional[str] = None
    input_message_stream: bool = False
    output_message_stream: bool = False


@dataclass
class ConnectionSignature:
    """Represents a connection target - only operation name is needed.

    In AI4EU blueprints, connections only specify the target operation name.
    The full signature details are looked up from the target node.
    """
    operation_name: str


@dataclass
class Connection:
    """Represents a connection to another node."""
    container_name: str
    operation_signature: ConnectionSignature


@dataclass
class OperationSignatureList:
    """Represents an operation with its connections."""
    operation_signature: OperationSignature
    connected_to: List[Connection] = field(default_factory=list)


@dataclass
class Node:
    """Represents a workflow node."""
    container_name: str
    image: str
    node_type: str
    operation_signature_list: List[OperationSignatureList]
    # Interface description for the portal. Optional: a standalone service
    # driven only over the HTTP control plane has no proto.
    proto_uri: Optional[str] = None
    
    # Runtime properties
    address: Optional[str] = None
    port: Optional[int] = None


@dataclass
class Blueprint:
    """Represents the complete workflow blueprint."""
    name: str
    pipeline_id: str
    creation_date: str
    type: str
    version: str
    nodes: List[Node]