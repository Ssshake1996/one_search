#!/usr/bin/env bash
set -euo pipefail
install_stage=preflight
trap 'printf '\''{"schema_version":1,"event":"installation_result","ok":false,"stage":"%s","error":{"code":"installation_failed","message":"Inspect stderr and retry the same command"}}\n'\'' "$install_stage" >&2' ERR
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(dirname -- "$script_dir")"
install_dir="${XDG_DATA_HOME:-$HOME/.local/share}/data-search/app"
data_dir="${XDG_DATA_HOME:-$HOME/.local/share}/data-search/data"
python_bin=python3
package_path=""
wheelhouse=""
model_dir=""
skip_model=0
no_autostart=0
roots=()
excludes=()
preset=balanced
while (($#)); do
  case "$1" in
    --root) roots+=("$2"); shift 2;;
    --exclude) excludes+=("$2"); shift 2;;
    --preset) preset="$2"; shift 2;;
    --install-dir) install_dir="$2"; shift 2;;
    --data-dir) data_dir="$2"; shift 2;;
    --python) python_bin="$2"; shift 2;;
    --package-path) package_path="$2"; shift 2;;
    --wheelhouse) wheelhouse="$2"; shift 2;;
    --model-dir) model_dir="$2"; shift 2;;
    --skip-model) skip_model=1; shift;;
    --no-autostart) no_autostart=1; shift;;
    --help) printf '%s\n' 'install.sh [--root DIRECTORY ...] [--exclude PATH ...] [--preset low|balanced|fast] [--install-dir DIR] [--data-dir DIR] [--python PYTHON] [--package-path WHEEL_OR_SOURCE] [--wheelhouse DIR] [--model-dir DIR | --skip-model] [--no-autostart]' 'Default first-install scope: this machine. Reinstall preserves existing scope.'; exit 0;;
    *) printf 'Unknown option: %s\n' "$1" >&2; exit 2;;
  esac
done
if [[ $skip_model == 1 && -n "$model_dir" ]]; then echo 'Choose --skip-model or --model-dir.' >&2; exit 2; fi
case "$preset" in low|balanced|fast) ;; *) echo 'Preset must be low, balanced or fast.' >&2; exit 2;; esac
"$python_bin" "$script_dir/check_runtime.py" "$repo_dir"
command -v flock >/dev/null || { echo 'flock (util-linux) is required to guard installation against concurrent model/daemon work.' >&2; exit 2; }
if [[ -z "$package_path" && -f "$repo_dir/RELEASE_MANIFEST.json" ]]; then
  package_path="$repo_dir/$("$python_bin" -c 'import json,sys; print(json.load(open(sys.argv[1]))["package_wheel"])' "$repo_dir/RELEASE_MANIFEST.json")"
  if [[ -z "$wheelhouse" ]]; then wheelhouse="$repo_dir/wheelhouse"; fi
fi
if [[ -z "$package_path" ]]; then package_path="$repo_dir"; fi
install_dir="$("$python_bin" -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$install_dir")"
data_dir="$("$python_bin" -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$data_dir")"
config_path="$data_dir/config.json"
for search_root in "${roots[@]}"; do [[ -d "$search_root" ]] || { printf 'Search root is not a directory: %s\n' "$search_root" >&2; exit 2; }; done
if [[ -n "$model_dir" ]]; then
  model_dir="$("$python_bin" -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$model_dir")"
  for asset in model.onnx tokenizer.json config.json manifest.json; do
    [[ -f "$model_dir/$asset" ]] || { printf 'Offline model directory is missing: %s\n' "$asset" >&2; exit 2; }
  done
fi
if [[ $no_autostart == 0 ]] && { ! command -v systemctl >/dev/null || ! systemctl --user show-environment >/dev/null 2>&1; }; then
  echo 'A working systemd user session is required for autostart. Use --no-autostart for an explicit manual-start installation.' >&2
  exit 2
fi
"$python_bin" - "$install_dir" "$data_dir" <<'PY'
import json, os, pathlib, sys
install, data = map(pathlib.Path, sys.argv[1:])
for p in (install, data):
    if p == pathlib.Path(p.anchor) or p == pathlib.Path.home():
        raise SystemExit(f'Refusing unsafe installation path: {p}')
    if any(x.is_symlink() for x in [p, *p.parents]):
        raise SystemExit(f'Installation path contains a symlink: {p}')
if data == install or install in data.parents or data in install.parents:
    raise SystemExit('Install and data directories must be separate and not nested.')
manifest = install / 'install-manifest.json'
marker = data / '.data-search-data.json'
if marker.exists():
    old_data = json.loads(marker.read_text())
    if old_data.get('product') != 'data-search' or old_data.get('data_dir') != str(data): raise SystemExit('Invalid data directory marker.')
elif data.exists() and any(data.iterdir()):
    raise SystemExit('Data directory must be empty or installer-managed.')
if manifest.exists():
    old = json.loads(manifest.read_text())
    if old.get('product') != 'data-search' or old.get('schema_version') != 1 or old.get('install_dir') != str(install) or old.get('data_dir') != str(data):
        raise SystemExit('Existing installation manifest does not match directories.')
    if old.get('autostart') == 'systemd-user':
        import subprocess
        subprocess.run(['systemctl','--user','stop',old['unit_name']],check=True)
elif install.exists() and any(install.iterdir()):
    raise SystemExit('Refusing nonempty unmanaged installation directory.')
install.mkdir(parents=True, exist_ok=True)
data.mkdir(parents=True, exist_ok=True)
os.chmod(data, 0o700)
marker.write_text(json.dumps(dict(product='data-search',schema_version=1,data_dir=str(data))))
manifest.write_text(json.dumps(dict(product='data-search',schema_version=1,install_dir=str(install),data_dir=str(data),config=str(data/'config.json'),autostart='none'), indent=2))
PY
venv_python="$install_dir/venv/bin/python"
cli="$install_dir/venv/bin/data-search"
install_stage=runtime
mkdir -p -- "$data_dir/model-job"
exec {model_manager_fd}>"$data_dir/model-job/manager.lock"
flock -n "$model_manager_fd" || { echo 'Another maintenance operation is active; retry later.' >&2; exit 1; }
if [[ -x "$venv_python" && -f "$data_dir/model-job/status.json" ]]; then
  "$venv_python" -m data_search.model_manager quiesce --config "$config_path"
fi
exec {model_worker_fd}>"$data_dir/model-job/worker.lock"
flock -n "$model_worker_fd" || { echo 'Model preparation is still running; retry later.' >&2; exit 1; }
if [[ -x "$cli" ]]; then "$cli" stop --config "$config_path"; fi
exec {daemon_fd}>"$data_dir/service.lock"
flock -n "$daemon_fd" || { echo 'The daemon is still running; retry later.' >&2; exit 1; }
if [[ ! -x "$venv_python" ]]; then "$python_bin" -m venv "$install_dir/venv"; fi
"$venv_python" "$script_dir/check_runtime.py" "$repo_dir"
echo 'Installing data-search and dependencies into its isolated environment...'
pip_args=(-m pip install --disable-pip-version-check --quiet)
if [[ -n "$wheelhouse" ]]; then pip_args+=(--no-index --find-links "$wheelhouse"); fi
"$venv_python" "${pip_args[@]}" "$package_path"
if [[ ! -f "$config_path" ]]; then
  init_args=(init --config "$config_path" --data-dir "$data_dir" --exclude "$install_dir")
  for search_root in "${roots[@]}"; do init_args+=(--root "$search_root"); done
  for excluded_path in "${excludes[@]}"; do init_args+=(--exclude "$excluded_path"); done
  "$cli" "${init_args[@]}"
  "$venv_python" - "$config_path" "$skip_model" "$model_dir" "$preset" <<'PY'
import json, pathlib, sys
from data_search.runtime_policy import apply_preset
p=pathlib.Path(sys.argv[1]); c=json.loads(p.read_text())
c=apply_preset(c,sys.argv[4])
if sys.argv[2]=='1': c['semantic']['enabled']=False
if sys.argv[3]: c['semantic']['model_dir']=sys.argv[3]
p.write_text(json.dumps(c,ensure_ascii=False,indent=2)); p.chmod(0o600)
PY
else echo "Preserving existing configuration: $config_path (roots and model options unchanged)."; fi
"$venv_python" - "$config_path" "$install_dir" <<'PY'
import json, pathlib, sys
p=pathlib.Path(sys.argv[1]); c=json.loads(p.read_text())
excluded=c.setdefault('exclude_paths', [])
if sys.argv[2] not in excluded:
    excluded.append(sys.argv[2]); p.write_text(json.dumps(c,ensure_ascii=False,indent=2)); p.chmod(0o600)
PY
"$venv_python" - "$install_dir" "$config_path" "$repo_dir/plugins/data-search" <<'PY'
import json,pathlib,shutil,sys
dest,config,source=map(pathlib.Path,sys.argv[1:])
mcp=dict(mcpServers={'data-search':dict(command=str(dest/'venv/bin/data-search'),args=['mcp','--config',str(config)])})
(dest/'mcp.json').write_text(json.dumps(mcp,indent=2))
if source.exists():
    shutil.copytree(source,dest/'plugin',dirs_exist_ok=True)
    (dest/'plugin/.mcp.json').write_text(json.dumps(mcp,indent=2))
PY
cp -- "$script_dir/uninstall.sh" "$install_dir/uninstall.sh"
chmod 700 "$install_dir/uninstall.sh"
unit_name="data-search-$("$venv_python" -c 'import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest()[:12])' "$install_dir").service"
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
flock -u "$daemon_fd"
flock -u "$model_worker_fd"
flock -u "$model_manager_fd"
exec {daemon_fd}>&-
exec {model_worker_fd}>&-
exec {model_manager_fd}>&-
if [[ $no_autostart == 0 ]]; then
  install_stage=daemon_start
  mkdir -p -- "$unit_dir"
  "$venv_python" - "$install_dir" "$config_path" "$unit_dir/$unit_name" <<'PY'
import pathlib,sys
install,config,unit=map(pathlib.Path,sys.argv[1:])
def quote(s): return '"'+str(s).replace('\\','\\\\').replace('"','\\"').replace('%','%%').replace('$','$$')+'"'
unit.write_text('[Unit]\nDescription=data_search local index service\nAfter=default.target\n\n[Service]\nType=simple\nExecStart='+quote(install/'venv/bin/data-search')+' daemon --config '+quote(config)+'\nRestart=on-failure\nRestartSec=5\nNice=10\nUMask=0077\n\n[Install]\nWantedBy=default.target\n')
PY
  systemctl --user daemon-reload
  systemctl --user enable --now "$unit_name"
else
  if command -v systemctl >/dev/null && systemctl --user show-environment >/dev/null 2>&1 && [[ -f "$unit_dir/$unit_name" ]]; then
    systemctl --user disable --now "$unit_name"
    rm -- "$unit_dir/$unit_name"
    systemctl --user daemon-reload
  fi
  "$cli" start --config "$config_path"
fi
"$venv_python" - "$install_dir/install-manifest.json" "$unit_name" "$unit_dir/$unit_name" "$no_autostart" <<'PY'
import json,pathlib,sys
from data_search import __version__
p=pathlib.Path(sys.argv[1]); m=json.loads(p.read_text()); m.update(version=__version__,cli=str(p.parent/'venv/bin/data-search'),unit_name=sys.argv[2],unit_path=sys.argv[3],autostart='none' if sys.argv[4]=='1' else 'systemd-user'); p.write_text(json.dumps(m,indent=2))
PY
for attempt in {1..20}; do if "$cli" status --config "$config_path" >/dev/null; then break; fi; sleep 0.25; done
install_stage=model_queued
if [[ $skip_model == 0 ]] && "$venv_python" -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1]))["semantic"]["enabled"] else 1)' "$config_path"; then
  if [[ -n "$model_dir" ]]; then
    active_model_dir="$("$venv_python" -c 'import json,sys; print(json.load(open(sys.argv[1]))["semantic"]["model_dir"])' "$config_path")"
    "$cli" model-import --config "$config_path" --source "$active_model_dir" || printf '%s\n' 'Basic search is running; offline model verification needs attention.' >&2
  else
    "$cli" model-start --config "$config_path" || printf '%s\n' 'Basic search is running; use model-status/model-start to recover model preparation.' >&2
  fi
fi
install_stage=acceptance
report="$("$cli" installation-status --config "$config_path" --install-dir "$install_dir")"
printf '%s\n' "$report" > "$data_dir/install-result.json"
chmod 600 "$data_dir/install-result.json"
printf '%s\n' "$report"
