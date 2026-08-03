"""Knowledge Store sidecar adapter.

Run as standalone service that wraps the Knowledge Store service.
"""

import sys
from pathlib import Path

# Add parent directories for imports (works for both local dev and Docker)
# In Docker: /app/common exists alongside /app/service.py
# Local dev: need to go up to portugal-node-sidecar/
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from common import knowledge_store_handlers, run

if __name__ == "__main__":
    run(knowledge_store_handlers, "Knowledge Store Adapter")
