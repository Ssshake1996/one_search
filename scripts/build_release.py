"""Build versioned bootstrap assets and, on Windows, an optional bundled runtime."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import shutil
import struct
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def runtime_metadata() -> dict:
    return {"python": f"{sys.version_info.major}.{sys.version_info.minor}",
            "implementation": platform.python_implementation().lower(),
            "system": platform.system().lower(), "machine": platform.machine().lower(),
            "bits": struct.calcsize("P") * 8}


def copy_documentation(repo: Path, bundle: Path) -> None:
    for directory in ("scripts", "plugins", "examples", "docs"):
        shutil.copytree(repo / directory, bundle / directory,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for filename in ("LICENSE", "NOTICE", "CHANGELOG.md"):
        if (repo / filename).is_file():
            shutil.copy2(repo / filename, bundle / filename)
    shutil.copy2(repo / "README.md", bundle / "PROJECT_README.md")


def finalize(bundle: Path, output: Path, manifest: dict) -> tuple[Path, Path]:
    manifest_path = bundle / "RELEASE_MANIFEST.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    checksums = {path.relative_to(bundle).as_posix(): digest(path)
                 for path in sorted(bundle.rglob("*")) if path.is_file() and path.name != "SHA256SUMS.json"}
    (bundle / "SHA256SUMS.json").write_text(json.dumps(checksums, indent=2) + "\n", encoding="utf-8")
    archive = output / (bundle.name + ".zip")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as handle:
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                handle.write(path, path.relative_to(output))
    external_manifest = output / (bundle.name + ".manifest.json")
    shutil.copy2(manifest_path, external_manifest)
    return archive, external_manifest


def bundle_readme(tag: str, native: bool) -> str:
    requirement = "No separately installed Python is required. The bundled executable includes CPython and application dependencies." if native else "Requires the matching installed CPython minor version and architecture shown below, including venv/pip. Dependency wheels are included."
    windows = tag.startswith("windows-")
    shell = "PowerShell" if windows else "Bash"
    language = "powershell" if windows else "bash"
    install = ".\\scripts\\install.ps1" if windows else "bash scripts/install.sh"
    selected = install + (" -Root @('D:\\Documents', 'D:\\Projects')" if windows else " --root /home/you/Documents --root /srv/projects")
    model_options = "`-SkipModel` or `-ModelDir`" if windows else "`--skip-model` or `--model-dir`"
    mcp_path = "%LOCALAPPDATA%\\data-search\\app\\mcp.json" if windows else "${XDG_DATA_HOME:-$HOME/.local/share}/data-search/app/mcp.json"
    return f"""# one_search release

Platform/runtime: `{tag}`.

{requirement}

## Install

Extract the entire archive, then run from its extracted directory in {shell}:

```{language}
{install}
```

The first installation defaults to all current-account-accessible local filesystems. To select directories instead:

```{language}
{selected}
```

Reinstallation preserves the saved scope. Use the settings window, or stop the service, edit config.json and restart to change it.
The installer automatically selects the bundled runtime or bundled wheel and dependency wheelhouse.
No Git checkout or source tree is needed. Installation starts a local background service and registers current-user login autostart; it does not configure the MCP host automatically.

Model weights are NOT included. Installation starts basic search first, then queues a detached pinned-model preparation job unless {model_options} is supplied to disable semantics or select an offline model directory. Model download failure does not block filename/keyword search; use model-status, model-start, model-import and model-cancel to inspect or recover preparation.
Python dependencies install offline from this bundle; the background model download needs network access by default. Read the repository README installation contract and DataDir/install-result.json for runtime, daemon, basic search and semantic readiness separately.

The native Windows installation provides `Settings.vbs` in the installed application directory. Open it to change search scope, content/semantic tiers and resource budgets, explicitly test changed database settings, or inspect queued work and service controls.
For a Python source installation use `python -m data_search.setup_ui --config <config.json>`.

Generated MCP configuration: `{mcp_path}`.
DeepSeek Harness uses the included [Cordis bundle](plugins/deepseek-harness/README.md). Registering the npm package adds the bundle; its first profile activation installs a missing backend and connects the official MCP client. An existing backend is reused.
Native Windows upgrades retain the previous runtime and a stopped pre-migration index/configuration snapshot under `.upgrade-*`. They require additional disk space and attempt rollback only for this installation's failed startup. Bootstrap/Linux upgrades do not use this transaction.
See [installation instructions](docs/INSTALL.md), [project README](PROJECT_README.md), and validation reports in docs/.

This release does not grant a new project source-code license. Dependencies keep their own licenses. The native runtime contains collected third-party license notices; wheel files retain their upstream metadata/licenses.
"""


def collect_licenses(destination: Path) -> None:
    destination.mkdir(parents=True)
    inventory = []
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name", "unknown")
        files = []
        for entry in distribution.files or []:
            if any(token in Path(entry).name.lower() for token in ("license", "copying", "notice")):
                source = Path(distribution.locate_file(entry))
                if source.is_file():
                    target = destination / name / str(entry).replace("../", "").replace("..\\", "")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
                    files.append(target.relative_to(destination).as_posix())
        inventory.append({"name": name, "version": distribution.version, "license": distribution.metadata.get("License-Expression") or distribution.metadata.get("License", ""), "notices": files})
    (destination / "inventory.json").write_text(json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8")
    for candidate in (Path(sys.base_prefix) / "LICENSE.txt", Path(sys.base_prefix) / "LICENSE"):
        if candidate.is_file():
            shutil.copy2(candidate, destination / "CPython-LICENSE.txt")
            break


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("dist/release"))
    parser.add_argument("--wheelhouse", type=Path, help="Existing dependency/build wheels, resolved without network")
    parser.add_argument("--native", action="store_true", help="Also build a Windows runtime that needs no installed Python")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    version = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    if args.native:
        try:
            installed_version = importlib.metadata.version("data-search")
        except importlib.metadata.PackageNotFoundError:
            installed_version = None
        if installed_version != version:
            raise SystemExit(f"Native build metadata is {installed_version!r}, expected {version}. Run: python -m pip install -e . --no-deps")
    runtime = runtime_metadata()
    if args.native and (runtime["system"] != "windows" or runtime["bits"] != 64):
        raise SystemExit("The native build currently targets 64-bit Windows only")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    tag = f'{runtime["system"]}-{runtime["machine"]}-py{sys.version_info.major}{sys.version_info.minor}'
    bundle = output / f"one-search-{version}-{tag}-bootstrap"
    if bundle.exists():
        raise SystemExit(f"Output already exists; choose a new output directory: {bundle}")
    wheels = bundle / "wheelhouse"
    wheels.mkdir(parents=True)
    command = [sys.executable, "-m", "pip", "wheel", "--disable-pip-version-check", "--quiet", "--wheel-dir", str(wheels)]
    if args.wheelhouse:
        command += ["--no-index", "--find-links", str(args.wheelhouse.resolve())]
    subprocess.run([*command, str(repo)], check=True)
    artifacts = sorted(wheels.glob("data_search-*.whl"))
    if len(artifacts) != 1:
        raise SystemExit("Expected exactly one data_search wheel")
    source_hashes = {}
    with zipfile.ZipFile(artifacts[0]) as wheel:
        for path in sorted((repo / "src").rglob("*.py")):
            raw = path.read_bytes()
            if wheel.read(path.relative_to(repo / "src").as_posix()) != raw:
                raise SystemExit(f"Source changed during build; rebuild: {path}")
            source_hashes[path.relative_to(repo).as_posix()] = hashlib.sha256(raw).hexdigest()
    source_hashes["pyproject.toml"] = digest(repo / "pyproject.toml")
    common = {"schema_version": 1, "product": "one_search", "version": version, "runtime": runtime,
              "model_included": False, "network_required_for_default_model_download": True,
              "default_scope": "machine", "source_sha256": source_hashes}
    copy_documentation(repo, bundle)
    (bundle / "README.md").write_text(bundle_readme(tag, False), encoding="utf-8")
    manifest = {**common, "kind": "python-bootstrap", "system_python_required": True,
                "package_wheel": artifacts[0].relative_to(bundle).as_posix(),
                "dependency_wheels": [path.name for path in sorted(wheels.glob("*.whl"))]}
    published = list(finalize(bundle, output, manifest))
    project_wheel = output / artifacts[0].name
    shutil.copy2(artifacts[0], project_wheel)
    published.append(project_wheel)
    if args.native:
        native = output / f"one-search-{version}-windows-{runtime['machine']}-native"
        native.mkdir()
        work = output / "native-build"
        build_command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--console",
                         "--name", "data-search", "--distpath", str(work / "dist"), "--workpath", str(work / "work"),
                         "--specpath", str(work), "--paths", str(repo / "src"), "--collect-submodules", "data_search",
                         "--recursive-copy-metadata", "data-search"]
        for package in ("onnxruntime", "tokenizers", "usearch", "psycopg", "psycopg_binary"):
            build_command += ["--collect-all", package]
        build_command += ["--collect-submodules", "mcp.server", "--collect-data", "mcp"]
        subprocess.run([*build_command, str(repo / "scripts/frozen_entry.py")], check=True)
        shutil.copytree(work / "dist/data-search", native / "runtime")
        copy_documentation(repo, native)
        collect_licenses(native / "THIRD_PARTY_LICENSES")
        (native / "README.md").write_text(bundle_readme(f"windows-{runtime['machine']}", True), encoding="utf-8")
        (native / "Install.cmd").write_text('@echo off\r\npowershell.exe -NoProfile -File "%~dp0scripts\\install.ps1" %*\r\npause\r\n', encoding="utf-8")
        native_manifest = {**common, "kind": "windows-native", "system_python_required": False,
                           "executable": "runtime/data-search.exe"}
        published.extend(finalize(native, output, native_manifest))
    checksums_path = output / "SHA256SUMS.txt"
    checksums_path.write_text("".join(f"{digest(path)}  {path.name}\n" for path in sorted(published)), encoding="utf-8")
    print(json.dumps({"version": version, "artifacts": [str(path) for path in [*published, checksums_path]]}, indent=2))


if __name__ == "__main__":
    main()
