"""Run the unchanged v0.6 lifecycle checks with the DSH 0.2 boot adapter."""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "service_controls_v060", HERE.parent / "v060/verify_service_controls_v060.py")
legacy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(legacy)


class Acceptance(legacy.Acceptance):
    def __init__(self, args):
        super().__init__(args)
        self.profile_worker = HERE / "dsh_service_profile_v070.mjs"
        self.report["acceptance_adapter"] = {
            "controller": "scripts/acceptance/v060/verify_service_controls_v060.py",
            "worker": "scripts/acceptance/v070/dsh_service_profile_v070.mjs",
            "host_boot_entry": "lib/profile-boot.js",
        }


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--dsh", type=Path, required=True)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--native-bundle", type=Path)
    parser.add_argument("--runtime-source-commit")
    parser.add_argument("--offline", action="store_true")
    acceptance = Acceptance(parser.parse_args())
    try:
        acceptance.run()
    except Exception as error:
        acceptance.report["passed"] = False
        acceptance.report["failure"] = {
            "type": type(error).__name__, "message": str(error)[-5000:]}
        raise
    finally:
        acceptance.cleanup()
        print(json.dumps({"passed": acceptance.report.get("passed"),
            "cleaned_up": acceptance.report.get("cleaned_up"),
            "report": str(acceptance.work / "report.json")}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
