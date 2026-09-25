"""Reject an incompatible Python before a bootstrap installation changes files."""
from __future__ import annotations

import json
import platform
import struct
import sys
from pathlib import Path


def check(bundle_dir: Path) -> None:
    if sys.version_info < (3, 11):
        raise ValueError("Python 3.11 or newer is required")
    manifest_path = bundle_dir / "RELEASE_MANIFEST.json"
    if not manifest_path.exists():
        return
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest["kind"] == "windows-native" and platform.system().lower() != "windows":
        raise ValueError("This native bundle requires Windows. Use a source installation on this operating system.")
    if manifest["kind"] != "python-bootstrap":
        return
    expected = manifest["runtime"]
    actual = {
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "implementation": platform.python_implementation().lower(),
        "system": platform.system().lower(),
        "machine": platform.machine().lower(),
        "bits": struct.calcsize("P") * 8,
    }
    for key, value in expected.items():
        if actual.get(key) != value:
            raise ValueError(f"This bundle requires {key}={value}; selected Python has {actual.get(key)}. Choose a matching Python or bundle.")


if __name__ == "__main__":
    try:
        check(Path(sys.argv[1]).resolve())
    except (ValueError, KeyError, OSError) as error:
        raise SystemExit(str(error))
