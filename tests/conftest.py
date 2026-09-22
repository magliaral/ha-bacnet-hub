from __future__ import annotations

import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Import the core before any test module imports voluptuous: HA 2026.9+
# installs its own validator package (probatio) under the ``voluptuous`` name
# at import time, and schemas built from a mix of both break (defaults are
# dropped, invalid values leak raw errors). At runtime the core is always
# imported first, so this only matters for the test process.
importlib.import_module("homeassistant")
