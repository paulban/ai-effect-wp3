"""Synthetic Data sidecar adapter.

Run as standalone service that wraps the Synthetic Data service.
"""

import sys
from pathlib import Path

# Add parent directories for imports (works for both local dev and Docker)
# In Docker: /app/common exists alongside /app/service.py
# Local dev: need to go up to portugal-node-sidecar/
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from common import synthetic_data_handlers, run

if __name__ == "__main__":
    run(synthetic_data_handlers, "Synthetic Data Adapter")
