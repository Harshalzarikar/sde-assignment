"""Test-only hard-kill hooks for crash-recovery exercises."""

from __future__ import annotations

import os
import sys
from typing import Any


CRASH_ENV = "CONTROLLER_CRASH_AT"


def crash_point(name: str, detail: Any = None) -> None:
    """Exit hard when CONTROLLER_CRASH_AT names this point; inert otherwise."""
    if os.environ.get(CRASH_ENV) != name:
        return
    print(f"[crash-hook] hard exit at {name}: {detail!r}", file=sys.stderr, flush=True)
    os._exit(137)
