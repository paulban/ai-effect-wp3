"""Unit tests for BlueprintParser."""

import json
import tempfile
from pathlib import Path

import pytest

from services.blueprint_parser import BlueprintParseError, BlueprintParser


def create_minimal_blueprint() -> dict:
    """Create a minimal valid blueprint."""
    return {
        "name": "Test Pipeline",
        "pipeline_id": "test-123",
        "creation_date": "2025-01-01",
        "type": "pipeline-topology/v2",
        "version": "2.0",
        "nodes": [
            {
                "container_name": "service-a",
                "proto_uri": "service-a.proto",
                "image": "service-a:latest",
                "node_type": "MLModel",
                "operation_signature_list": [
                    {
                        "operation_signature": {
                            "operation_name": "Process",
                            "input_message_name": "Input",
                            "output_message_name": "Output",
                        },
                        "connected_to": [],
                    }
                ],
            }
        ],
    }


def create_chain_blueprint() -> dict:
    """Create blueprint with chain topology: A -> B -> C."""
    return {
        "name": "Chain Pipeline",
        "pipeline_id": "chain-123",
        "creation_date": "2025-01-01",
        "type": "pipeline-topology/v2",
        "version": "2.0",
        "nodes": [
            {
                "container_name": "service-a",
                "proto_uri": "a.proto",
                "image": "a:latest",
                "node_type": "MLModel",
                "operation_signature_list": [
                    {
                        "operation_signature": {
                            "operation_name": "ProcessA",
                            "input_message_name": "InputA",
                            "output_message_name": "OutputA",
                        },
                        "connected_to": [
                            {
                                "container_name": "service-b",
                                "operation_signature": {"operation_name": "ProcessB"},
                            }
                        ],
                    }
                ],
            },
            {
                "container_name": "service-b",
                "proto_uri": "b.proto",
                "image": "b:latest",
                "node_type": "MLModel",
                "operation_signature_list": [
                    {
                        "operation_signature": {
                            "operation_name": "ProcessB",
                            "input_message_name": "InputB",
                            "output_message_name": "OutputB",
                        },
                        "connected_to": [
                            {
                                "container_name": "service-c",
                                "operation_signature": {"operation_name": "ProcessC"},
                            }
                        ],
                    }
                ],
            },
            {
                "container_name": "service-c",
                "proto_uri": "c.proto",
                "image": "c:latest",
                "node_type": "MLModel",
                "operation_signature_list": [
                    {
                        "operation_signature": {
                            "operation_name": "ProcessC",
                            "input_message_name": "InputC",
                            "output_message_name": "OutputC",
                        },
                        "connected_to": [],
                    }
                ],
            },
        ],
    }


def create_parallel_blueprint() -> dict:
    """Create blueprint with parallel topology: A, B (no connections)."""
    return {
        "name": "Parallel Pipeline",
        "pipeline_id": "parallel-123",
        "creation_date": "2025-01-01",
        "type": "pipeline-topology/v2",
        "version": "2.0",
        "nodes": [
            {
                "container_name": "service-a",
                "proto_uri": "a.proto",
                "image": "a:latest",
                "node_type": "MLModel",
                "operation_signature_list": [
                    {
                        "operation_signature": {
                            "operation_name": "ProcessA",
                            "input_message_name": "Input",
                            "output_message_name": "Output",
                        },
                        "connected_to": [],
                    }
                ],
            },
            {
                "container_name": "service-b",
                "proto_uri": "b.proto",
                "image": "b:latest",
                "node_type": "MLModel",
                "operation_signature_list": [
                    {
                        "operation_signature": {
                            "operation_name": "ProcessB",
                            "input_message_name": "Input",
                            "output_message_name": "Output",
                        },
                        "connected_to": [],
                    }
                ],
            },
        ],
    }


def create_fan_out_blueprint() -> dict:
    """Create blueprint with fan-out: A -> B, A -> C."""
    return {
        "name": "Fan-out Pipeline",
        "pipeline_id": "fanout-123",
        "creation_date": "2025-01-01",
        "type": "pipeline-topology/v2",
        "version": "2.0",
        "nodes": [
            {
                "container_name": "service-a",
                "proto_uri": "a.proto",
                "image": "a:latest",
                "node_type": "MLModel",
                "operation_signature_list": [
                    {
                        "operation_signature": {
                            "operation_name": "ProcessA",
                            "input_message_name": "Input",
                            "output_message_name": "Output",
                        },
                        "connected_to": [
                            {
                                "container_name": "service-b",
                                "operation_signature": {"operation_name": "ProcessB"},
                            },
                            {
                                "container_name": "service-c",
                                "operation_signature": {"operation_name": "ProcessC"},
                            },
                        ],
                    }
                ],
            },
            {
                "container_name": "service-b",
                "proto_uri": "b.proto",
                "image": "b:latest",
                "node_type": "MLModel",
                "operation_signature_list": [
                    {
                        "operation_signature": {
                            "operation_name": "ProcessB",
                            "input_message_name": "Input",
                            "output_message_name": "Output",
                        },
                        "connected_to": [],
                    }
                ],
            },
            {
                "container_name": "service-c",
                "proto_uri": "c.proto",
                "image": "c:latest",
                "node_type": "MLModel",
                "operation_signature_list": [
                    {
                        "operation_signature": {
                            "operation_name": "ProcessC",
                            "input_message_name": "Input",
                            "output_message_name": "Output",
                        },
                        "connected_to": [],
                    }
                ],
            },
        ],
    }


def create_fan_in_blueprint() -> dict:
    """Create blueprint with fan-in: A -> C, B -> C."""
    return {
        "name": "Fan-in Pipeline",
        "pipeline_id": "fanin-123",
        "creation_date": "2025-01-01",
        "type": "pipeline-topology/v2",
        "version": "2.0",
        "nodes": [
            {
                "container_name": "service-a",
                "proto_uri": "a.proto",
                "image": "a:latest",
                "node_type": "MLModel",
                "operation_signature_list": [
                    {
                        "operation_signature": {
                            "operation_name": "ProcessA",
                            "input_message_name": "Input",
                            "output_message_name": "Output",
                        },
                        "connected_to": [
                            {
                                "container_name": "service-c",
                                "operation_signature": {"operation_name": "ProcessC"},
                            }
                        ],
                    }
                ],
            },
            {
                "container_name": "service-b",
                "proto_uri": "b.proto",
                "image": "b:latest",
                "node_type": "MLModel",
                "operation_signature_list": [
                    {
                        "operation_signature": {
                            "operation_name": "ProcessB",
                            "input_message_name": "Input",
                            "output_message_name": "Output",
                        },
                        "connected_to": [
                            {
                                "container_name": "service-c",
                                "operation_signature": {"operation_name": "ProcessC"},
                            }
                        ],
                    }
                ],
            },
            {
                "container_name": "service-c",
                "proto_uri": "c.proto",
                "image": "c:latest",
                "node_type": "MLModel",
                "operation_signature_list": [
                    {
                        "operation_signature": {
                            "operation_name": "ProcessC",
                            "input_message_name": "Input",
                            "output_message_name": "Output",
                        },
                        "connected_to": [],
                    }
                ],
            },
        ],
    }


def create_diamond_blueprint() -> dict:
    """Create blueprint with diamond: A -> B, A -> C, B -> D, C -> D."""
    return {
        "name": "Diamond Pipeline",
        "pipeline_id": "diamond-123",
        "creation_date": "2025-01-01",
        "type": "pipeline-topology/v2",
        "version": "2.0",
        "nodes": [
            {
                "container_name": "service-a",
                "proto_uri": "a.proto",
                "image": "a:latest",
                "node_type": "MLModel",
                "operation_signature_list": [
                    {
                        "operation_signature": {
                            "operation_name": "ProcessA",
                            "input_message_name": "Input",
                            "output_message_name": "Output",
                        },
                        "connected_to": [
                            {
                                "container_name": "service-b",
                                "operation_signature": {"operation_name": "ProcessB"},
                            },
                            {
                                "container_name": "service-c",
                                "operation_signature": {"operation_name": "ProcessC"},
                            },
                        ],
                    }
                ],
            },
            {
                "container_name": "service-b",
                "proto_uri": "b.proto",
                "image": "b:latest",
                "node_type": "MLModel",
                "operation_signature_list": [
                    {
                        "operation_signature": {
                            "operation_name": "ProcessB",
                            "input_message_name": "Input",
                            "output_message_name": "Output",
                        },
                        "connected_to": [
                            {
                                "container_name": "service-d",
                                "operation_signature": {"operation_name": "ProcessD"},
                            }
                        ],
                    }
                ],
            },
            {
                "container_name": "service-c",
                "proto_uri": "c.proto",
                "image": "c:latest",
                "node_type": "MLModel",
                "operation_signature_list": [
                    {
                        "operation_signature": {
                            "operation_name": "ProcessC",
                            "input_message_name": "Input",
                            "output_message_name": "Output",
                        },
                        "connected_to": [
                            {
                                "container_name": "service-d",
                                "operation_signature": {"operation_name": "ProcessD"},
                            }
                        ],
                    }
                ],
            },
            {
                "container_name": "service-d",
                "proto_uri": "d.proto",
                "image": "d:latest",
                "node_type": "MLModel",
                "operation_signature_list": [
                    {
                        "operation_signature": {
                            "operation_name": "ProcessD",
                            "input_message_name": "Input",
                            "output_message_name": "Output",
                        },
                        "connected_to": [],
                    }
                ],
            },
        ],
    }


@pytest.fixture
def parser():
    return BlueprintParser()


class TestParseJson:
    """Tests for parse_json method."""

    def test_parse_minimal_blueprint(self, parser):
        """Parse minimal valid blueprint."""
        data = create_minimal_blueprint()
        graph = parser.parse_json(data)

        assert len(graph.all_nodes) == 1
        assert len(graph.start_nodes) == 1
        assert "service-a:Process" in graph.all_nodes

    def test_parse_chain_topology(self, parser):
        """Parse chain topology: A -> B -> C."""
        data = create_chain_blueprint()
        graph = parser.parse_json(data)

        assert len(graph.all_nodes) == 3
        assert len(graph.start_nodes) == 1
        assert graph.start_nodes[0].key == "service-a:ProcessA"

        node_a = graph.all_nodes["service-a:ProcessA"]
        node_b = graph.all_nodes["service-b:ProcessB"]
        node_c = graph.all_nodes["service-c:ProcessC"]

        assert len(node_a.next_nodes) == 1
        assert node_a.next_nodes[0].key == "service-b:ProcessB"
        assert len(node_b.dependencies) == 1
        assert node_b.dependencies[0].key == "service-a:ProcessA"
        assert len(node_c.next_nodes) == 0

    def test_parse_parallel_topology(self, parser):
        """Parse parallel topology: A, B (no connections)."""
        data = create_parallel_blueprint()
        graph = parser.parse_json(data)

        assert len(graph.all_nodes) == 2
        assert len(graph.start_nodes) == 2

        start_keys = {n.key for n in graph.start_nodes}
        assert "service-a:ProcessA" in start_keys
        assert "service-b:ProcessB" in start_keys

    def test_parse_fan_out_topology(self, parser):
        """Parse fan-out: A -> B, A -> C."""
        data = create_fan_out_blueprint()
        graph = parser.parse_json(data)

        assert len(graph.all_nodes) == 3
        assert len(graph.start_nodes) == 1

        node_a = graph.all_nodes["service-a:ProcessA"]
        assert len(node_a.next_nodes) == 2

        next_keys = {n.key for n in node_a.next_nodes}
        assert "service-b:ProcessB" in next_keys
        assert "service-c:ProcessC" in next_keys

    def test_parse_fan_in_topology(self, parser):
        """Parse fan-in: A -> C, B -> C."""
        data = create_fan_in_blueprint()
        graph = parser.parse_json(data)

        assert len(graph.all_nodes) == 3
        assert len(graph.start_nodes) == 2

        node_c = graph.all_nodes["service-c:ProcessC"]
        assert len(node_c.dependencies) == 2

        dep_keys = {n.key for n in node_c.dependencies}
        assert "service-a:ProcessA" in dep_keys
        assert "service-b:ProcessB" in dep_keys

    def test_parse_diamond_topology(self, parser):
        """Parse diamond: A -> B, A -> C, B -> D, C -> D."""
        data = create_diamond_blueprint()
        graph = parser.parse_json(data)

        assert len(graph.all_nodes) == 4
        assert len(graph.start_nodes) == 1
        assert graph.start_nodes[0].key == "service-a:ProcessA"

        node_d = graph.all_nodes["service-d:ProcessD"]
        assert len(node_d.dependencies) == 2

    def test_none_data_raises(self, parser):
        """Parse None raises ValueError."""
        with pytest.raises(ValueError, match="data is required"):
            parser.parse_json(None)


class TestValidation:
    """Tests for validation errors."""

    def test_missing_name_raises(self, parser):
        """Missing name field raises error."""
        data = create_minimal_blueprint()
        del data["name"]

        with pytest.raises(BlueprintParseError, match="Invalid blueprint structure"):
            parser.parse_json(data)

    def test_missing_pipeline_id_raises(self, parser):
        """Missing pipeline_id field raises error."""
        data = create_minimal_blueprint()
        del data["pipeline_id"]

        with pytest.raises(BlueprintParseError, match="Invalid blueprint structure"):
            parser.parse_json(data)

    def test_missing_nodes_raises(self, parser):
        """Missing nodes field raises error."""
        data = create_minimal_blueprint()
        del data["nodes"]

        with pytest.raises(BlueprintParseError, match="Invalid blueprint structure"):
            parser.parse_json(data)

    def test_empty_nodes_raises(self, parser):
        """Empty nodes array raises error."""
        data = create_minimal_blueprint()
        data["nodes"] = []

        with pytest.raises(BlueprintParseError, match="Invalid blueprint structure"):
            parser.parse_json(data)

    def test_empty_name_raises(self, parser):
        """Empty name field raises error."""
        data = create_minimal_blueprint()
        data["name"] = ""

        with pytest.raises(BlueprintParseError, match="Invalid blueprint structure"):
            parser.parse_json(data)

    def test_missing_container_name_raises(self, parser):
        """Missing container_name in node raises error."""
        data = create_minimal_blueprint()
        del data["nodes"][0]["container_name"]

        with pytest.raises(BlueprintParseError, match="Invalid blueprint structure"):
            parser.parse_json(data)

    def test_missing_operation_name_raises(self, parser):
        """Missing operation_name raises error."""
        data = create_minimal_blueprint()
        del data["nodes"][0]["operation_signature_list"][0]["operation_signature"][
            "operation_name"
        ]

        with pytest.raises(BlueprintParseError, match="Invalid blueprint structure"):
            parser.parse_json(data)

    def test_invalid_connection_target_raises(self, parser):
        """Connection to non-existent node raises error."""
        data = create_minimal_blueprint()
        data["nodes"][0]["operation_signature_list"][0]["connected_to"] = [
            {
                "container_name": "nonexistent",
                "operation_signature": {"operation_name": "Process"},
            }
        ]

        with pytest.raises(BlueprintParseError, match="Invalid connection target"):
            parser.parse_json(data)

    def test_mutual_dependency_no_start_nodes_raises(self, parser):
        """Mutual dependency (A <-> B) has no start nodes."""
        data = {
            "name": "Mutual",
            "pipeline_id": "mutual-123",
            "creation_date": "2025-01-01",
            "type": "pipeline-topology/v2",
            "version": "2.0",
            "nodes": [
                {
                    "container_name": "service-a",
                    "proto_uri": "a.proto",
                    "image": "a:latest",
                    "node_type": "MLModel",
                    "operation_signature_list": [
                        {
                            "operation_signature": {
                                "operation_name": "ProcessA",
                                "input_message_name": "Input",
                                "output_message_name": "Output",
                            },
                            "connected_to": [
                                {
                                    "container_name": "service-b",
                                    "operation_signature": {"operation_name": "ProcessB"},
                                }
                            ],
                        }
                    ],
                },
                {
                    "container_name": "service-b",
                    "proto_uri": "b.proto",
                    "image": "b:latest",
                    "node_type": "MLModel",
                    "operation_signature_list": [
                        {
                            "operation_signature": {
                                "operation_name": "ProcessB",
                                "input_message_name": "Input",
                                "output_message_name": "Output",
                            },
                            "connected_to": [
                                {
                                    "container_name": "service-a",
                                    "operation_signature": {"operation_name": "ProcessA"},
                                }
                            ],
                        }
                    ],
                },
            ],
        }

        with pytest.raises(BlueprintParseError, match="No start nodes found"):
            parser.parse_json(data)

    def test_cycle_reachable_from_start_raises(self, parser):
        """Cycle reachable from start node raises circular dependency error."""
        # Topology: Start -> A -> B -> A (cycle in A-B, but Start is entry point)
        data = {
            "name": "Circular",
            "pipeline_id": "circular-123",
            "creation_date": "2025-01-01",
            "type": "pipeline-topology/v2",
            "version": "2.0",
            "nodes": [
                {
                    "container_name": "start",
                    "proto_uri": "start.proto",
                    "image": "start:latest",
                    "node_type": "MLModel",
                    "operation_signature_list": [
                        {
                            "operation_signature": {
                                "operation_name": "Start",
                                "input_message_name": "Input",
                                "output_message_name": "Output",
                            },
                            "connected_to": [
                                {
                                    "container_name": "service-a",
                                    "operation_signature": {"operation_name": "ProcessA"},
                                }
                            ],
                        }
                    ],
                },
                {
                    "container_name": "service-a",
                    "proto_uri": "a.proto",
                    "image": "a:latest",
                    "node_type": "MLModel",
                    "operation_signature_list": [
                        {
                            "operation_signature": {
                                "operation_name": "ProcessA",
                                "input_message_name": "Input",
                                "output_message_name": "Output",
                            },
                            "connected_to": [
                                {
                                    "container_name": "service-b",
                                    "operation_signature": {"operation_name": "ProcessB"},
                                }
                            ],
                        }
                    ],
                },
                {
                    "container_name": "service-b",
                    "proto_uri": "b.proto",
                    "image": "b:latest",
                    "node_type": "MLModel",
                    "operation_signature_list": [
                        {
                            "operation_signature": {
                                "operation_name": "ProcessB",
                                "input_message_name": "Input",
                                "output_message_name": "Output",
                            },
                            "connected_to": [
                                {
                                    "container_name": "service-a",
                                    "operation_signature": {"operation_name": "ProcessA"},
                                }
                            ],
                        }
                    ],
                },
            ],
        }

        with pytest.raises(BlueprintParseError, match="Circular dependency detected"):
            parser.parse_json(data)


class TestParseFile:
    """Tests for parse_file method."""

    def test_parse_file(self, parser):
        """Parse blueprint from file."""
        data = create_minimal_blueprint()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(data, f)
            temp_path = f.name

        try:
            graph = parser.parse_file(temp_path)
            assert len(graph.all_nodes) == 1
        finally:
            Path(temp_path).unlink()

    def test_parse_file_not_found_raises(self, parser):
        """Parse non-existent file raises error."""
        with pytest.raises(FileNotFoundError, match="Blueprint file not found"):
            parser.parse_file("/nonexistent/path.json")

    def test_parse_file_empty_path_raises(self, parser):
        """Parse empty path raises error."""
        with pytest.raises(ValueError, match="path is required"):
            parser.parse_file("")

    def test_parse_invalid_json_raises(self, parser):
        """Parse invalid JSON raises error."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            f.write("not valid json {")
            temp_path = f.name

        try:
            with pytest.raises(BlueprintParseError, match="Invalid JSON"):
                parser.parse_file(temp_path)
        finally:
            Path(temp_path).unlink()


class TestGraphStructure:
    """Tests for graph structure correctness."""

    def test_node_has_correct_properties(self, parser):
        """Parsed node has correct properties."""
        data = create_minimal_blueprint()
        graph = parser.parse_json(data)

        node = graph.all_nodes["service-a:Process"]
        assert node.node.container_name == "service-a"
        assert node.node.proto_uri == "service-a.proto"
        assert node.node.image == "service-a:latest"
        assert node.node.node_type == "MLModel"

    def test_node_without_proto_uri(self, parser):
        """A node without a proto (HTTP-only service) parses with proto_uri None."""
        data = create_minimal_blueprint()
        del data["nodes"][0]["proto_uri"]
        graph = parser.parse_json(data)

        assert graph.all_nodes["service-a:Process"].node.proto_uri is None

    def test_operation_has_correct_signature(self, parser):
        """Parsed operation has correct signature."""
        data = create_minimal_blueprint()
        graph = parser.parse_json(data)

        node = graph.all_nodes["service-a:Process"]
        op = node.operation.operation_signature
        assert op.operation_name == "Process"
        assert op.input_message_name == "Input"
        assert op.output_message_name == "Output"

    def test_leaf_nodes_have_no_next(self, parser):
        """Leaf nodes have empty next_nodes."""
        data = create_chain_blueprint()
        graph = parser.parse_json(data)

        leaf_nodes = graph.get_leaf_nodes()
        assert len(leaf_nodes) == 1
        assert leaf_nodes[0].key == "service-c:ProcessC"
