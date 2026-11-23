"""演练公共常量。"""
from __future__ import annotations

import os

WINDOW_SECONDS = int(os.environ.get("CANARY_WINDOW_SECONDS", "30"))
SAMPLE_SIZE = 10
STEP_PCT = 10
