"""Parser for AI-Effect blueprint.json files."""

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, field_validator

from models.graph import ExecutionGraph, GraphNode
from models.node import Connection, ConnectionSignature, Node, OperationSignature, OperationSignatureList


class BlueprintParseError(Exception):
    """Raised when blueprint parsing fails."""

    pass


class BlueprintOperationSignature(BaseModel):
    """Full operation signature with message type details.

    Message names are optional since some nodes may not have inputs
    (data generators) or outputs (data sinks).
    """

    model_config = ConfigDict(extra="forbid")

    operation_name: str
    input_message_name: str | None = None
    output_message_name: str | None = None
    input_message_stream: bool = False
    output_message_stream: bool = False

    @field_validator("operation_name")
    @classmethod
    def operation_name_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("operation_name is required")
        return v

    @field_validator("input_message_name", "output_message_name", mode="before")
    @classmethod
    def empty_string_to_none(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            return None
        return v


class BlueprintConnectionSignature(BaseModel):
    """Connection target identifier.

    AI4EU portal only exports the operation name for connections.
    Full signature details are looked up from the target node.
    """

    model_config = ConfigDict(extra="forbid")

    operation_name: str

    @field_validator("operation_name")
    @classmethod
    def operation_name_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("operation_name is required")
        return v


class BlueprintConnection(BaseModel):
    """Pydantic model for connection in blueprint JSON."""

    model_config = ConfigDict(extra="forbid")

    container_name: str
    operation_signature: BlueprintConnectionSignature

    @field_validator("container_name")
    @classmethod
    def container_name_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("container_name is required")
        return v


class BlueprintOperationList(BaseModel):
    """Pydantic model for operation list in blueprint JSON."""

    model_config = ConfigDict(extra="forbid")

    operation_signature: BlueprintOperationSignature
    connected_to: list[BlueprintConnection] = []


class BlueprintNode(BaseModel):
    """Pydantic model for node in blueprint JSON."""

    model_config = ConfigDict(extra="forbid")

    container_name: str
    proto_uri: str | None = None
    image: str
    node_type: str
    operation_signature_list: list[BlueprintOperationList]

    @field_validator("container_name", "image", "node_type")
    @classmethod
    def field_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("field is required")
        return v

    @field_validator("operation_signature_list")
    @classmethod
    def operations_not_empty(cls, v: list) -> list:
        if not v:
            raise ValueError("operation_signature_list is required")
        return v


class BlueprintSchema(BaseModel):
    """Pydantic model for blueprint JSON structure."""

    model_config = ConfigDict(extra="forbid")

    name: str
    pipeline_id: str
    creation_date: str
    type: str
    version: str
    nodes: list[BlueprintNode]

    @field_validator("name", "pipeline_id", "type", "version")
    @classmethod
    def field_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("field is required")
        return v

    @field_validator("nodes")
    @classmethod
    def nodes_not_empty(cls, v: list) -> list:
        if not v:
            raise ValueError("nodes is required")
        return v


class BlueprintParser:
    """Parses blueprint.json into ExecutionGraph."""

    def parse_file(self, path: str) -> ExecutionGraph:
        """Parse blueprint from JSON file."""
        if not path:
            raise ValueError("path is required")

        file_path = Path(path)
        if not file_path.exists():
            raise FileNotFoundError(f"Blueprint file not found: {path}")

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            raise BlueprintParseError(f"Invalid JSON: {e}")

        return self.parse_json(data)

    def parse_json(self, data: dict) -> ExecutionGraph:
        """Parse blueprint from JSON dict."""
        if data is None:
            raise ValueError("data is required")

        try:
            schema = BlueprintSchema.model_validate(data)
        except Exception as e:
            raise BlueprintParseError(f"Invalid blueprint structure: {e}")

        self._validate_connections(schema)
        graph = self._build_graph(schema)
        self._detect_cycles(graph)

        return graph

    def _validate_connections(self, schema: BlueprintSchema) -> None:
        """Validate all connections reference existing nodes."""
        valid_targets: set[str] = set()

        for node in schema.nodes:
            for op in node.operation_signature_list:
                key = f"{node.container_name}:{op.operation_signature.operation_name}"
                valid_targets.add(key)

        for node in schema.nodes:
            for op in node.operation_signature_list:
                for conn in op.connected_to:
                    target_key = (
                        f"{conn.container_name}:{conn.operation_signature.operation_name}"
                    )
                    if target_key not in valid_targets:
                        raise BlueprintParseError(
                            f"Invalid connection target: {target_key}"
                        )

    def _build_graph(self, schema: BlueprintSchema) -> ExecutionGraph:
        """Build ExecutionGraph from parsed blueprint."""
        graph = ExecutionGraph()
        node_map: dict[str, GraphNode] = {}

        # First pass: create all graph nodes
        for bp_node in schema.nodes:
            node = self._create_node(bp_node)
            for bp_op in bp_node.operation_signature_list:
                op_list = self._create_operation_list(bp_op)
                graph_node = GraphNode(node=node, operation=op_list)
                node_map[graph_node.key] = graph_node
                graph.add_node(graph_node)

        # Second pass: connect nodes
        for bp_node in schema.nodes:
            for bp_op in bp_node.operation_signature_list:
                source_key = (
                    f"{bp_node.container_name}:{bp_op.operation_signature.operation_name}"
                )
                source_node = node_map[source_key]

                for conn in bp_op.connected_to:
                    target_key = (
                        f"{conn.container_name}:{conn.operation_signature.operation_name}"
                    )
                    target_node = node_map[target_key]

                    source_node.next_nodes.append(target_node)
                    target_node.dependencies.append(source_node)

        # Identify start nodes
        start_nodes = [n for n in graph.all_nodes.values() if not n.dependencies]
        if not start_nodes:
            raise BlueprintParseError("No start nodes found")

        graph.start_nodes = start_nodes
        return graph

    def _create_node(self, bp_node: BlueprintNode) -> Node:
        """Create Node dataclass from blueprint node."""
        operations = [
            self._create_operation_list(op) for op in bp_node.operation_signature_list
        ]
        return Node(
            container_name=bp_node.container_name,
            proto_uri=bp_node.proto_uri,
            image=bp_node.image,
            node_type=bp_node.node_type,
            operation_signature_list=operations,
        )

    def _create_operation_list(self, bp_op: BlueprintOperationList) -> OperationSignatureList:
        """Create OperationSignatureList from blueprint operation."""
        op_sig = OperationSignature(
            operation_name=bp_op.operation_signature.operation_name,
            input_message_name=bp_op.operation_signature.input_message_name,
            output_message_name=bp_op.operation_signature.output_message_name,
            input_message_stream=bp_op.operation_signature.input_message_stream,
            output_message_stream=bp_op.operation_signature.output_message_stream,
        )

        connections = [
            Connection(
                container_name=conn.container_name,
                operation_signature=ConnectionSignature(
                    operation_name=conn.operation_signature.operation_name,
                ),
            )
            for conn in bp_op.connected_to
        ]

        return OperationSignatureList(
            operation_signature=op_sig,
            connected_to=connections,
        )

    def _detect_cycles(self, graph: ExecutionGraph) -> None:
        """Detect circular dependencies in graph."""
        visited: set[str] = set()
        rec_stack: set[str] = set()

        def has_cycle(node: GraphNode) -> bool:
            visited.add(node.key)
            rec_stack.add(node.key)

            for next_node in node.next_nodes:
                if next_node.key not in visited:
                    if has_cycle(next_node):
                        return True
                elif next_node.key in rec_stack:
                    return True

            rec_stack.remove(node.key)
            return False

        for start_node in graph.start_nodes:
            if start_node.key not in visited:
                if has_cycle(start_node):
                    raise BlueprintParseError("Circular dependency detected")
