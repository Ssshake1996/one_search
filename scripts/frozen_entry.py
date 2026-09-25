"""PyInstaller entry point; the internal worker protocol stays on standard I/O."""
from __future__ import annotations

import sys


def main():
    args = sys.argv[1:]
    if args[:1] == ["--internal-module"]:
        if len(args) < 2 or args[1] not in {"data_search", "data_search.worker"}:
            raise SystemExit("Unknown internal module")
        module, args = args[1], args[2:]
        sys.argv = [module, *args]
        if module == "data_search.worker":
            from data_search.worker import main as worker_main
            return worker_main()
    if not args or args[:1] == ["setup"]:
        from data_search.runtime import configure_native_threads
        configure_native_threads()
        from data_search.setup_ui import main as setup_main
        return setup_main(args[1:] if args else [])
    from data_search.cli import main as cli_main
    return cli_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
