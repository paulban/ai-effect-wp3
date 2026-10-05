"""Minimal user-facing gateway for the Danish Node workflow.

This is intentionally lightweight: it accepts a user prompt, submits the
workflow to the orchestrator, exposes the recommendation, and lets the user
confirm or reject the selected topology before downstream work continues.
The orchestrator remains the real workflow engine.

The goal is to integrate all of the services and components involved in the Danish Node workflow into a cohesive UI.
"""

from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path
from typing import Any

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

ORCHESTRATOR_URL = os.environ.get("ORCHESTRATOR_URL", "http://host.docker.internal:18000")
EXPORT_PATH = "/workspace/use-cases/denmark-node/export"
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))

# Initialize FastAPI application
app = FastAPI(title="Danish Node Web UI", version="1.0.0")
TOPOLOGY_CONFIRMATIONS: dict[str, str | None] = {}


class PromptRequest(BaseModel):
    prompt: str


class ConfirmRequest(BaseModel):
    workflow_id: str
    selected_signature: str | None = None
    accepted: bool = False


class WorkflowStatusResponse(BaseModel):
  #INFO: maybe later session information will be included
  workflow_id: str
  status: str
  recommendation: dict[str, Any] | None = None
  tasks: list[dict[str, Any]] | None = None
  error: str | None = None


def _read_export() -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        with open(f"{EXPORT_PATH}/blueprint.json", "r", encoding="utf-8") as fp:
            blueprint = json.load(fp)
        with open(f"{EXPORT_PATH}/dockerinfo.json", "r", encoding="utf-8") as fp:
            dockerinfo = json.load(fp)
        return blueprint, dockerinfo
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=503,
            #TODO: If a button to run the export script is added (Should we?) to the UI, update this message accordingly.
            detail="Workflow export files are not ready yet. Run ./generate-export.sh first.",
        ) from exc

# Poll the orchestrator for the status of a workflow until it completes or fails, or until the timeout is reached.
def _poll_workflow(workflow_id: str, timeout_seconds: int = 60) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            response = requests.get(f"{ORCHESTRATOR_URL}/workflows/{workflow_id}/tasks", timeout=10)
            if response.status_code == 200:
                data = response.json()
                statuses = [task.get("status") for task in data.get("tasks", [])]
                if statuses and (
                  all(status == "completed" for status in statuses)
                  or any(status == "failed" for status in statuses)
                ):
                    return data
            elif response.status_code == 404:
                raise HTTPException(status_code=404, detail="Workflow not found")
        except requests.RequestException:
            pass
        time.sleep(1)
    return {"tasks": []}



# Load the recommendation from the list of tasks, if available.
def _load_recommendation(task_list: list[dict[str, Any]]) -> dict[str, Any] | None:
    for task in task_list:
        for ref in task.get("output_refs", []):
            uri = ref.get("uri")
            if not uri:
                continue
            try:
                with open(uri, "r", encoding="utf-8") as fp:
                    payload_data = json.load(fp)
                if isinstance(payload_data, dict) and payload_data.get("report"):
                    if payload_data.get("recommended_network"):
                        return {
                            "report": payload_data["report"],
                          "run_config": payload_data.get("run_config", {}),
                          "config_report": payload_data.get("config_report", {}),
                            "recommended_network": payload_data["recommended_network"],
                            "drawings": payload_data.get("drawings", []),
                        }
            except Exception:
                continue
    return None


# Add review links to the recommendation.
def _add_review_links(workflow_id: str, recommendation: dict[str, Any] | None) -> dict[str, Any] | None:
    if not recommendation:
        return None
    review = dict(recommendation)
    signature = review["recommended_network"].get("signature")
    if signature and any(drawing.get("signature") == signature for drawing in review.get("drawings", [])):
        review["recommended_drawing_url"] = f"/workflows/{workflow_id}/drawings/{signature}"
    return review



# Serve the main HTML page for the Danish Node workflow UI.
@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    html = """
    <html>
      <head>
        <title>Danish Node Workflow</title>
        <style>
          body { font-family: Arial, sans-serif; margin: 32px; background: #f5f7fb; }
          .card { max-width: 920px; margin: auto; background: white; padding: 20px; border-radius: 12px; box-shadow: 0 2px 10px rgba(0,0,0,0.06); }
          textarea { width: 100%; min-height: 120px; margin-top: 8px; }
          button { margin-top: 12px; padding: 10px 16px; cursor: pointer; }
          .result { margin-top: 16px; padding: 12px; border: 1px solid #dfe5ec; border-radius: 8px; background: #f9fbff; }
          .row { display: flex; gap: 12px; align-items: center; }
          .topology-image { display: block; width: 100%; margin-top: 12px; border: 1px solid #dfe5ec; background: white; }
        </style>
      </head>
      <body>
        <div class="card">
          <h2>Danish Node - topology recommendation</h2>
          <p>Submit a brief description of the target experiment and the workflow will generate a topology recommendation.</p>
          <textarea id="prompt" placeholder="Example: Use the CHP at 310-D as supply and feed the heat dumpload on 716-D."></textarea>
          <div class="row">
            <button id="submitBtn">Submit workflow</button>
            <button id="confirmBtn" style="display:none;">Confirm recommended topology</button>
            <button id="rejectBtn" style="display:none;">Reject</button>
          </div>
          <div id="status" class="result">Waiting for input.</div>
          <div id="recommendation" class="result" style="display:none;"></div>
        </div>
        <script>
          const statusEl = document.getElementById('status');
          const recEl = document.getElementById('recommendation');
          const submitBtn = document.getElementById('submitBtn');
          const confirmBtn = document.getElementById('confirmBtn');
          const rejectBtn = document.getElementById('rejectBtn');
          let workflowId = null;
          let selectedSignature = null;

          function showMessage(text, isError = false) {
            statusEl.innerHTML = text;
            statusEl.style.borderColor = isError ? '#de6b6b' : '#dfe5ec';
            statusEl.style.background = isError ? '#fff4f4' : '#f9fbff';
          }

          submitBtn.onclick = async () => {
            const prompt = document.getElementById('prompt').value.trim();
            if (!prompt) {
              showMessage('Please enter a prompt first.', true);
              return;
            }
            submitBtn.disabled = true;
            showMessage('Submitting workflow...');
            const response = await fetch('/submit', {
              method: 'POST',
              headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({ prompt })
            });
            const data = await response.json();
            submitBtn.disabled = false;
            if (!response.ok) {
              showMessage(data.detail || 'Submission failed.', true);
              return;
            }
            workflowId = data.workflow_id;
            showMessage(`Workflow started: ${workflowId}. Waiting for topology recommendation...`);
            if (data.recommendation) {
              renderRecommendation(data.recommendation);
            } else {
              recEl.style.display = 'none';
              setTimeout(pollForRecommendation, 2000);
            }
          };

          function renderRecommendation(data) {
            const report = data.report || data;
            const network = data.recommended_network || null;
            const runConfig = data.run_config || {};
            const assumptions = data.config_report?.assumptions || [];
            selectedSignature = network?.signature || report.recommended_network || null;
            const networkDetails = network ? JSON.stringify(network, null, 2) : 'No network details are available yet.';
            const selection = report.selection || {};
            const recommended = selection.recommended || {};
            const nextBest = selection.next_best || {};
            const comparison = recommended.signature ?
              '<h4>Why this network was selected</h4>' +
              '<p>Selected from ' + (selection.candidate_count || 1) + ' feasible options using ' + (selection.criteria || []).join(', ') + '.</p>' +
              '<p><strong>Selected:</strong> ' + recommended.pipe_count + ' pipes, ' + recommended.pipe_m + ' m total pipe, ' + recommended.trench_m + ' m trench.' +
              (nextBest.signature ? ' <strong>Next option:</strong> ' + nextBest.pipe_count + ' pipes, ' + nextBest.pipe_m + ' m total pipe.' : '') + '</p>' : '';
            const drawing = data.recommended_drawing_url ? '<h4>Network drawing</h4><img class="topology-image" src="' + data.recommended_drawing_url + '" alt="Recommended topology ' + selectedSignature + '">' : '';
            recEl.innerHTML =
              '<h3>Recommended topology</h3>' +
              '<p><strong>Signature:</strong> ' + (selectedSignature || 'unavailable') + '</p>' +
              '<p>' + (report.reason || report.summary || '') + '</p>' +
              comparison + drawing +
              (assumptions.length ? '<h4>Configuration assumptions</h4><ul>' + assumptions.map(item => '<li>' + item + '</li>').join('') + '</ul>' : '') +
              '<h4>Interpreted configuration</h4><pre>' + JSON.stringify(runConfig, null, 2) + '</pre>' +
              '<h4>Network configuration</h4><pre>' + networkDetails + '</pre>';
            recEl.style.display = 'block';
            confirmBtn.style.display = 'inline-block';
            rejectBtn.style.display = 'inline-block';
            showMessage('Recommendation received. Review and confirm the topology before continuing.');
          }

          async function pollForRecommendation() {
            if (!workflowId) return;
            const status = await fetch(`/workflows/${workflowId}`);
            const statusData = await status.json();
            if (!status.ok || statusData.status === 'failed') {
              showMessage(statusData.error || statusData.detail || 'Workflow failed before producing a recommendation.', true);
              return;
            }
            if (statusData.recommendation?.recommended_network) {
              renderRecommendation(statusData.recommendation);
              return;
            }
            if (statusData.status === 'completed') {
              showMessage('Workflow completed, but no generated topology was found.', true);
              return;
            }
            setTimeout(pollForRecommendation, 2000);
          }

          confirmBtn.onclick = async () => {
            if (!workflowId) return;
            const response = await fetch('/confirm', {
              method: 'POST',
              headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({ workflow_id: workflowId, selected_signature: selectedSignature, accepted: true })
            });
            const data = await response.json();
            showMessage(data.message || 'Topology accepted.', false);
          };

          rejectBtn.onclick = async () => {
            if (!workflowId) return;
            const response = await fetch('/confirm', {
              method: 'POST',
              headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({ workflow_id: workflowId, selected_signature: selectedSignature, accepted: false })
            });
            const data = await response.json();
            showMessage(data.message || 'Topology rejected.', false);
          };
        </script>
      </body>
    </html>
    """
    return HTMLResponse(content=html)


# Health check endpoint. It uses a simple GET request to verify that the service is running.
@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# Endpoint to retrieve a specific drawing for a given workflow and signature.
@app.get("/workflows/{workflow_id}/drawings/{signature}")
def recommended_drawing(workflow_id: str, signature: str) -> FileResponse:
  response = requests.get(f"{ORCHESTRATOR_URL}/workflows/{workflow_id}/tasks", timeout=20)
  if response.status_code >= 400:
    raise HTTPException(status_code=response.status_code, detail=response.text)

  recommendation = _load_recommendation(response.json().get("tasks", []))
  if not recommendation:
    raise HTTPException(status_code=404, detail="Generated topology is not available")

  drawing = next(
    (item for item in recommendation.get("drawings", []) if item.get("signature") == signature),
    None,
  )
  if not drawing:
    raise HTTPException(status_code=404, detail="Topology drawing is not available")

  drawing_path = Path(drawing["uri"]).resolve()
  workflow_directory = (DATA_DIR / workflow_id).resolve()
  if workflow_directory not in drawing_path.parents or not drawing_path.is_file():
    raise HTTPException(status_code=404, detail="Topology drawing is not available")
  return FileResponse(drawing_path, media_type="image/svg+xml")


# Endpoint to submit a new prompt to the orchestrator and initiate a workflow.
# TODO: Currently workflows are not tied to user sessions, so any user can potentially interact with any workflow. We want to implement proper session management to ensure that only authorized users can access and modify their own workflows.
@app.post("/submit")
def submit_prompt(payload: PromptRequest) -> dict[str, Any]:
    blueprint, dockerinfo = _read_export()
    payload_json = {"prompt": payload.prompt}
    payload_b64 = base64.b64encode(json.dumps(payload_json).encode("utf-8")).decode("utf-8")

    request_body = {
        "blueprint": blueprint,
        "dockerinfo": dockerinfo,
        "inputs": [{"protocol": "inline", "uri": payload_b64, "format": "json"}],
    }

    response = requests.post(f"{ORCHESTRATOR_URL}/workflows", json=request_body, timeout=20)
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=response.text)

    data = response.json()
    workflow_id = data["workflow_id"]
    tasks = _poll_workflow(workflow_id)
    recommendation = _add_review_links(workflow_id, _load_recommendation(tasks.get("tasks", [])))

    return {"workflow_id": workflow_id, "status": "running", "recommendation": recommendation}


# Endpoint to confirm the topology for a given workflow.
@app.post("/confirm")
def confirm_topology(payload: ConfirmRequest) -> dict[str, str | None]:
    if not payload.workflow_id:
        raise HTTPException(status_code=400, detail="workflow_id is required")

    TOPOLOGY_CONFIRMATIONS[payload.workflow_id] = payload.selected_signature

    if payload.accepted:
        return {
            "status": "accepted",
            "message": (
                f"Topology confirmed for workflow {payload.workflow_id}."
                f" Selected signature: {payload.selected_signature or 'unspecified'}"
            ),
            "selected_signature": payload.selected_signature,
        }

    return {
        "status": "rejected",
        "message": (
            f"Topology rejected for workflow {payload.workflow_id}. "
            "A new recommendation can be generated."
        ),
        "selected_signature": payload.selected_signature,
    }


# Endpoint to retrieve the status of a specific workflow.
@app.get("/workflows/{workflow_id}")
def workflow_status(workflow_id: str) -> WorkflowStatusResponse:
    response = requests.get(f"{ORCHESTRATOR_URL}/workflows/{workflow_id}/tasks", timeout=20)
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=response.text)

    data = response.json()
    tasks = data.get("tasks", [])
    recommendation = _add_review_links(workflow_id, _load_recommendation(tasks))
    failure = next((task.get("error") for task in tasks if task.get("status") == "failed"), None)

    status = "running"
    if tasks and all(task.get("status") == "completed" for task in tasks):
        status = "completed"
    elif tasks and any(task.get("status") == "failed" for task in tasks):
        status = "failed"

    return WorkflowStatusResponse(
        workflow_id=workflow_id,
        status=status,
        recommendation=recommendation,
        tasks=tasks,
        error=failure,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8080")))
