"""Import paths for the benchmark service's tests.

The service imports its own modules as `benchmark` and the shared control plane
as `common`, which in the image sit side by side under /app. Outside the image
they live one and three directories up respectively, so both roots go on the
path before any test module is collected.
"""

import sys
from pathlib import Path

SERVICE_DIRECTORY = Path(__file__).resolve().parents[1]
USE_CASES_DIRECTORY = SERVICE_DIRECTORY.parents[2]

for import_path in (SERVICE_DIRECTORY, USE_CASES_DIRECTORY):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))
