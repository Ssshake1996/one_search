"""PyInstaller entry point; the internal worker protocol stays on standard I/O."""
from __future__ import annotations

import sys


def main():
    args = sys.argv[1:]
    if args[:1] == ["--internal-module"]:
        if len(args) < 2 or args[1] not in {"data_search", "data_search.worker", "data_search.preflight", "data_search.upgrade", "data_search.host_integration", "data_search.model_manager", "data_search.installation"}:
            raise SystemExit("Unknown internal module")
        module, args = args[1], args[2:]
        sys.argv = [module, *args]
        if module in {"data_search.model_manager", "data_search.installation"}:
            import importlib
            return importlib.import_module(module).main(args)
        if module == "data_search.worker":
            from data_search.worker import main as worker_main
            return worker_main()
        if module == "data_search.preflight":
            from data_search.preflight import main as preflight_main
            return preflight_main()
        if module == "data_search.upgrade":
            from data_search.upgrade import main as upgrade_main
            return upgrade_main()
        if module == "data_search.host_integration":
            from data_search.host_integration import main as host_main
            return host_main()
    if not args or args[:1] == ["setup"]:
        from data_search.runtime import configure_native_threads
        configure_native_threads()
        from data_search.setup_ui import main as setup_main
        return setup_main(args[1:] if args else [])
    from data_search.cli import main as cli_main
    return cli_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
