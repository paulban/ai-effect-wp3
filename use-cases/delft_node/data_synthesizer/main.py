"""Synthetic Power Grid Service - AI-Effect Orchestrator Adapter.

This service wraps the Chung-Lu-Chain power grid synthesizer package
and exposes it via the AI-Effect control interface.

Pipeline:
    grid_configurator (ConfigureGrid) -> grid_synthesizer (SynthesizeGrid)

Endpoints:
    POST /control/execute     - Start an operation (ConfigureGrid or SynthesizeGrid)
    GET  /control/status/{id} - Check task status
    GET  /control/output/{id} - Retrieve task output (DataReference)
    GET  /control/data/{id}   - Serve raw data (JSON)
    GET  /health              - Health check
"""

from common import synth_handlers, run

if __name__ == "__main__":
    run(synth_handlers, "Synthetic Power Grid")
