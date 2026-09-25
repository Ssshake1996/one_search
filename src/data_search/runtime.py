"""Commands work both in a Python environment and the bundled executable."""
from __future__ import annotations

import os
import sys


def configure_native_threads(count: int = 1) -> None:
    """Set native pool bounds before a CLI daemon imports NumPy or model code."""
    value = str(max(1, min(int(count), 2)))
    for variable in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[variable] = value


def process_command(module: str, *args: str) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--internal-module", module, *args]
    return [sys.executable, "-m", module, *args]
