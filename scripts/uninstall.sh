#!/usr/bin/env bash
set -euo pipefail
install_dir="${XDG_DATA_HOME:-$HOME/.local/share}/data-search/app"
delete_data=0
while (($#)); do
  case "$1" in
    --install-dir) install_dir="$2"; shift 2;;
    --delete-data) delete_data=1; shift;;
    --help) echo 'uninstall.sh [--install-dir DIR] [--delete-data]'; exit 0;;
    *) echo "Unknown argument: $1" >&2; exit 2;;
  esac
done
[[ -d "$install_dir" ]] || { echo 'Already uninstalled.'; exit 0; }
python_bin="$install_dir/venv/bin/python"
[[ -x "$python_bin" ]] || python_bin=python3
"$python_bin" - "$install_dir" "$delete_data" <<'PY'
import json, os, pathlib, shutil, subprocess, sys
install=pathlib.Path(sys.argv[1]).absolute(); manifest=install/'install-manifest.json'
if not manifest.is_file(): raise SystemExit('No install manifest; refusing deletion.')
m=json.loads(manifest.read_text()); data=pathlib.Path(m['data_dir'])
if m.get('product')!='data-search' or m.get('schema_version')!=1 or m['install_dir']!=str(install): raise SystemExit('Invalid manifest; refusing deletion.')
for p in (install,data):
    if p != p.absolute() or p == pathlib.Path(p.anchor) or p == pathlib.Path.home() or any(x.is_symlink() for x in [p,*p.parents]): raise SystemExit(f'Unsafe deletion target: {p}')
if data==install or data in install.parents or install in data.parents: raise SystemExit('Overlapping directories; refusing deletion.')
if sys.argv[2]=='1' and data.exists():
    marker=data/'.data-search-data.json'
    if not marker.is_file(): raise SystemExit('Data marker missing; data retained.')
    mark=json.loads(marker.read_text())
    if mark.get('product')!='data-search' or mark.get('data_dir')!=str(data): raise SystemExit('Invalid data marker; data retained.')
from data_search.config import load_config
from data_search.maintenance import MaintenanceGuard, purge_external_index
config=load_config(m['config'])
with MaintenanceGuard(config,stop=True):
    if sys.argv[2]=='1': purge_external_index(config)
    if m.get('autostart')=='systemd-user':
        expected_dir=pathlib.Path(os.environ.get('XDG_CONFIG_HOME',str(pathlib.Path.home()/'.config')))/'systemd/user'
        unit=pathlib.Path(m['unit_path'])
        if unit.parent!=expected_dir or unit.name!=m['unit_name'] or not unit.name.startswith('data-search-'): raise SystemExit('Invalid unit path.')
        subprocess.run(['systemctl','--user','disable','--now',m['unit_name']],check=True)
        unit.unlink(missing_ok=True)
        subprocess.run(['systemctl','--user','daemon-reload'],check=True)
    shutil.rmtree(install)
    if sys.argv[2]=='1' and data.exists():
        shutil.rmtree(data); print('Uninstalled and removed configured data directory and managed external index files.')
    else: print(f'Uninstalled. Configuration, models and indexes preserved at {data}')
PY
