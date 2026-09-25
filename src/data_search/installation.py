"""Small installation acceptance report; indexing completion is a separate state."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import atomic_json, load_config
from .model_manager import model_status
from .service import rpc, service_status


def installation_status(config, install_dir=None):
    runtime = True
    if install_dir:
        manifest = Path(install_dir) / 'install-manifest.json'
        try:
            value = json.loads(manifest.read_text(encoding='utf-8-sig'))
            runtime = value.get('product') == 'data-search' and Path(value.get('cli', '')).is_file()
        except (OSError, ValueError):
            runtime = False
    result = {'schema_version': 1, 'event': 'installation_result', 'runtime_installed': runtime,
              'daemon_running': False, 'basic_search_ready': False, 'dsh_connection': 'not_checked',
              'config': config.get('config_path'), 'data_dir': config['data_dir'], 'error': None,
              'semantic': model_status(config)}
    try:
        service_status(config)
        result['daemon_running'] = True
        response = rpc(config, 'search', {'query': 'one_search_installation_probe_93c2f7', 'mode': 'files', 'limit': 1}, timeout=15)
        result['basic_search_ready'] = isinstance(response, dict) and isinstance(response.get('results'), list)
        index = rpc(config, 'index_status', timeout=15)
        result['coverage'] = index.get('coverage', {})
    except Exception as error:
        result['error'] = {'code': 'service_probe_failed', 'type': type(error).__name__,
                           'message': 'Run start, status and installation-status with the same configuration'}
    result['ok'] = runtime and result['daemon_running'] and result['basic_search_ready']
    result['indexing_complete'] = None  # Successful service probes never certify whole-machine completeness.
    result['indexing_completion_note'] = 'Inspect coverage and per-source status; first discovery may still be running'
    return result


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--install-dir')
    parser.add_argument('--write-result', action='store_true')
    args = parser.parse_args(argv)
    config = load_config(args.config)
    result = installation_status(config, args.install_dir)
    if args.write_result:
        atomic_json(Path(config['data_dir']) / 'install-result.json', result)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
