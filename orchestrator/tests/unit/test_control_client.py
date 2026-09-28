"""Unit tests for ControlClient."""

import pytest
from pytest_httpx import HTTPXMock

from models.data_reference import DataReference, Format, Protocol
from services.control_client import (
    ControlClient,
    ControlClientError,
    ExecuteRequest,
    ExecuteResponse,
    OutputResponse,
    StatusResponse,
)


@pytest.fixture
def client():
    return ControlClient(timeout=5.0)


@pytest.fixture
def sample_input():
    return DataReference(
        protocol=Protocol.S3,
        uri="s3://bucket/input.csv",
        format=Format.CSV,
    )


@pytest.fixture
def sample_output():
    return DataReference(
        protocol=Protocol.S3,
        uri="s3://bucket/output.json",
        format=Format.JSON,
    )


class TestControlClientInit:
    """Tests for ControlClient initialization."""

    def test_init_with_timeout(self):
        """Create client with custom timeout."""
        client = ControlClient(timeout=10.0)
        assert client._timeout == 10.0

    def test_init_default_timeout(self):
        """Create client with default timeout."""
        client = ControlClient()
        assert client._timeout == 30.0

    def test_init_zero_timeout_raises(self):
        """Zero timeout raises error."""
        with pytest.raises(ValueError, match="timeout must be positive"):
            ControlClient(timeout=0)

    def test_init_negative_timeout_raises(self):
        """Negative timeout raises error."""
        with pytest.raises(ValueError, match="timeout must be positive"):
            ControlClient(timeout=-1)


class TestExecute:
    """Tests for execute method."""

    def test_execute_sync_complete(self, client, httpx_mock: HTTPXMock, sample_output):
        """Execute returns complete with output."""
        httpx_mock.add_response(
            method="POST",
            url="http://service:8080/control/execute",
            json={
                "status": "complete",
                "output": sample_output.model_dump(mode="json"),
            },
        )

        response = client.execute(
            base_url="http://service:8080",
            method="ProcessData",
            workflow_id="wf-123",
            task_id="task-456",
        )

        assert response.status == "complete"
        assert response.output is not None
        assert response.output.uri == "s3://bucket/output.json"

    def test_execute_async_running(self, client, httpx_mock: HTTPXMock):
        """Execute returns running with task_id."""
        httpx_mock.add_response(
            method="POST",
            url="http://service:8080/control/execute",
            json={
                "status": "running",
                "task_id": "service-task-789",
            },
        )

        response = client.execute(
            base_url="http://service:8080",
            method="TrainModel",
            workflow_id="wf-123",
            task_id="task-456",
        )

        assert response.status == "running"
        assert response.task_id == "service-task-789"
        assert response.output is None

    def test_execute_failed(self, client, httpx_mock: HTTPXMock):
        """Execute returns failed with error."""
        httpx_mock.add_response(
            method="POST",
            url="http://service:8080/control/execute",
            json={
                "status": "failed",
                "error": "Invalid input data",
            },
        )

        response = client.execute(
            base_url="http://service:8080",
            method="ProcessData",
            workflow_id="wf-123",
            task_id="task-456",
        )

        assert response.status == "failed"
        assert response.error == "Invalid input data"

    def test_execute_with_inputs(
        self, client, httpx_mock: HTTPXMock, sample_input, sample_output
    ):
        """Execute sends inputs correctly."""
        httpx_mock.add_response(
            method="POST",
            url="http://service:8080/control/execute",
            json={
                "status": "complete",
                "output": sample_output.model_dump(mode="json"),
            },
        )

        response = client.execute(
            base_url="http://service:8080",
            method="ProcessData",
            workflow_id="wf-123",
            task_id="task-456",
            inputs=[sample_input],
        )

        assert response.status == "complete"

        # Verify request body
        request = httpx_mock.get_request()
        import json

        body = json.loads(request.content)
        assert body["method"] == "ProcessData"
        assert body["workflow_id"] == "wf-123"
        assert body["task_id"] == "task-456"
        assert len(body["inputs"]) == 1
        assert body["inputs"][0]["uri"] == "s3://bucket/input.csv"

    def test_execute_trailing_slash_url(
        self, client, httpx_mock: HTTPXMock, sample_output
    ):
        """Execute handles trailing slash in base_url."""
        httpx_mock.add_response(
            method="POST",
            url="http://service:8080/control/execute",
            json={
                "status": "complete",
                "output": sample_output.model_dump(mode="json"),
            },
        )

        response = client.execute(
            base_url="http://service:8080/",
            method="ProcessData",
            workflow_id="wf-123",
            task_id="task-456",
        )

        assert response.status == "complete"

    def test_execute_http_error_raises(self, client, httpx_mock: HTTPXMock):
        """HTTP 500 raises ControlClientError."""
        httpx_mock.add_response(
            method="POST",
            url="http://service:8080/control/execute",
            status_code=500,
            text="Internal Server Error",
        )

        with pytest.raises(ControlClientError, match="HTTP 500"):
            client.execute(
                base_url="http://service:8080",
                method="ProcessData",
                workflow_id="wf-123",
                task_id="task-456",
            )

    def test_execute_invalid_response_raises(self, client, httpx_mock: HTTPXMock):
        """Invalid JSON response raises ControlClientError."""
        httpx_mock.add_response(
            method="POST",
            url="http://service:8080/control/execute",
            json={"invalid": "response"},
        )

        with pytest.raises(ControlClientError, match="Invalid response"):
            client.execute(
                base_url="http://service:8080",
                method="ProcessData",
                workflow_id="wf-123",
                task_id="task-456",
            )

    def test_execute_empty_base_url_raises(self, client):
        """Empty base_url raises ValueError."""
        with pytest.raises(ValueError, match="base_url is required"):
            client.execute(
                base_url="",
                method="ProcessData",
                workflow_id="wf-123",
                task_id="task-456",
            )

    def test_execute_empty_method_raises(self, client):
        """Empty method raises ValueError."""
        with pytest.raises(ValueError, match="method is required"):
            client.execute(
                base_url="http://service:8080",
                method="",
                workflow_id="wf-123",
                task_id="task-456",
            )

    def test_execute_empty_workflow_id_raises(self, client):
        """Empty workflow_id raises ValueError."""
        with pytest.raises(ValueError, match="workflow_id is required"):
            client.execute(
                base_url="http://service:8080",
                method="ProcessData",
                workflow_id="",
                task_id="task-456",
            )

    def test_execute_empty_task_id_raises(self, client):
        """Empty task_id raises ValueError."""
        with pytest.raises(ValueError, match="task_id is required"):
            client.execute(
                base_url="http://service:8080",
                method="ProcessData",
                workflow_id="wf-123",
                task_id="",
            )


class TestGetStatus:
    """Tests for get_status method."""

    def test_get_status_running(self, client, httpx_mock: HTTPXMock):
        """Get status returns running."""
        httpx_mock.add_response(
            method="GET",
            url="http://service:8080/control/status/task-123",
            json={
                "status": "running",
                "progress": 45,
            },
        )

        response = client.get_status(
            base_url="http://service:8080",
            task_id="task-123",
        )

        assert response.status == "running"
        assert response.progress == 45

    def test_get_status_complete(self, client, httpx_mock: HTTPXMock):
        """Get status returns complete."""
        httpx_mock.add_response(
            method="GET",
            url="http://service:8080/control/status/task-123",
            json={
                "status": "complete",
            },
        )

        response = client.get_status(
            base_url="http://service:8080",
            task_id="task-123",
        )

        assert response.status == "complete"

    def test_get_status_failed(self, client, httpx_mock: HTTPXMock):
        """Get status returns failed with error."""
        httpx_mock.add_response(
            method="GET",
            url="http://service:8080/control/status/task-123",
            json={
                "status": "failed",
                "error": "Out of memory",
            },
        )

        response = client.get_status(
            base_url="http://service:8080",
            task_id="task-123",
        )

        assert response.status == "failed"
        assert response.error == "Out of memory"

    def test_get_status_http_error_raises(self, client, httpx_mock: HTTPXMock):
        """HTTP 404 raises ControlClientError."""
        httpx_mock.add_response(
            method="GET",
            url="http://service:8080/control/status/task-123",
            status_code=404,
            text="Not Found",
        )

        with pytest.raises(ControlClientError, match="HTTP 404"):
            client.get_status(
                base_url="http://service:8080",
                task_id="task-123",
            )

    def test_get_status_empty_base_url_raises(self, client):
        """Empty base_url raises ValueError."""
        with pytest.raises(ValueError, match="base_url is required"):
            client.get_status(base_url="", task_id="task-123")

    def test_get_status_empty_task_id_raises(self, client):
        """Empty task_id raises ValueError."""
        with pytest.raises(ValueError, match="task_id is required"):
            client.get_status(base_url="http://service:8080", task_id="")


class TestGetOutput:
    """Tests for get_output method."""

    def test_get_output_success(self, client, httpx_mock: HTTPXMock, sample_output):
        """Get output returns output reference."""
        httpx_mock.add_response(
            method="GET",
            url="http://service:8080/control/output/task-123",
            json={
                "output": sample_output.model_dump(mode="json"),
            },
        )

        response = client.get_output(
            base_url="http://service:8080",
            task_id="task-123",
        )

        assert response.output.uri == "s3://bucket/output.json"
        assert response.output.format == Format.JSON

    def test_get_output_http_error_raises(self, client, httpx_mock: HTTPXMock):
        """HTTP 404 raises ControlClientError."""
        httpx_mock.add_response(
            method="GET",
            url="http://service:8080/control/output/task-123",
            status_code=404,
            text="Not Found",
        )

        with pytest.raises(ControlClientError, match="HTTP 404"):
            client.get_output(
                base_url="http://service:8080",
                task_id="task-123",
            )

    def test_get_output_empty_base_url_raises(self, client):
        """Empty base_url raises ValueError."""
        with pytest.raises(ValueError, match="base_url is required"):
            client.get_output(base_url="", task_id="task-123")

    def test_get_output_empty_task_id_raises(self, client):
        """Empty task_id raises ValueError."""
        with pytest.raises(ValueError, match="task_id is required"):
            client.get_output(base_url="http://service:8080", task_id="")


class TestModels:
    """Tests for request/response models."""

    def test_execute_request_valid(self, sample_input):
        """Create valid ExecuteRequest."""
        req = ExecuteRequest(
            method="ProcessData",
            workflow_id="wf-123",
            task_id="task-456",
            inputs=[sample_input],
            parameters={"batch_size": 100},
        )
        assert req.method == "ProcessData"
        assert req.workflow_id == "wf-123"
        assert len(req.inputs) == 1

    def test_execute_response_complete(self, sample_output):
        """Create complete ExecuteResponse."""
        resp = ExecuteResponse(status="complete", output=sample_output)
        assert resp.status == "complete"
        assert resp.output == sample_output

    def test_execute_response_running(self):
        """Create running ExecuteResponse."""
        resp = ExecuteResponse(status="running", task_id="service-task-123")
        assert resp.status == "running"
        assert resp.task_id == "service-task-123"

    def test_status_response_running(self):
        """Create running StatusResponse."""
        resp = StatusResponse(status="running", progress=50)
        assert resp.status == "running"
        assert resp.progress == 50

    def test_output_response_valid(self, sample_output):
        """Create valid OutputResponse."""
        resp = OutputResponse(output=sample_output)
        assert resp.output == sample_output
