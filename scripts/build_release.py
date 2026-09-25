"""Build a platform-specific Python bootstrap bundle, without claiming a native installer.

Requires Python 3.11+ and pip on the build host. By default resolves/downloads
dependency wheels. Build on each target OS/architecture; native wheels are not portable.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("dist"))
    parser.add_argument("--wheelhouse", type=Path, help="Use existing wheelhouse without network; must include all dependencies and build requirements")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    tag = f"{platform.system().lower()}-{platform.machine().lower()}-py{sys.version_info.major}{sys.version_info.minor}"
    bundle = output / f"data-search-bootstrap-{tag}"
    if bundle.exists():
        raise SystemExit(f"Output already exists; choose a new output directory: {bundle}")
    wheels = bundle / "wheelhouse"
    wheels.mkdir(parents=True)
    command = [sys.executable, "-m", "pip", "wheel", "--disable-pip-version-check", "--quiet", "--wheel-dir", str(wheels)]
    if args.wheelhouse:
        command += ["--no-index", "--find-links", str(args.wheelhouse.resolve())]
    command.append(str(repo))
    subprocess.run(command, check=True)
    artifacts = sorted(wheels.glob("data_search-*.whl"))
    if len(artifacts) != 1:
        raise SystemExit("Expected exactly one data_search wheel.")
    for directory in ("scripts", "plugins", "examples"):
        shutil.copytree(repo / directory, bundle / directory, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(repo / "docs", bundle / "docs", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    if (repo / "README.md").is_file():
        shutil.copy2(repo / "README.md", bundle / "PROJECT_README.md")
    wheel_relative = artifacts[0].relative_to(bundle).as_posix()
    instructions = f"""# data_search bootstrap release\n\nPlatform: {tag}. Requires an installed Python {sys.version_info.major}.{sys.version_info.minor} with venv/pip.\nThis is not a self-contained native executable. It includes dependency wheels for the build platform.\nModel assets are downloaded during installation unless --skip-model / -SkipModel or an offline model directory is provided.\n\nWindows (PowerShell, choose the actual directory to index):\n```powershell\n.\\scripts\\install.ps1 -Root 'C:\\Users\\you\\Documents' -PackagePath '.\\{wheel_relative.replace('/', chr(92))}' -Wheelhouse '.\\wheelhouse'\n```\n\nLinux (choose the actual directory to index):\n```bash\nbash scripts/install.sh --root /home/you/Documents --package-path '{wheel_relative}' --wheelhouse wheelhouse\n```\n\nSee docs/INSTALL.md for offline models, manual autostart, MCP wiring and uninstall.\n"""
    if (repo / "README.md").is_file():
        instructions += "\nProject capabilities and scope: [project README](PROJECT_README.md). Validation reports are in docs/.\n"
    (bundle / "README.md").write_text(instructions, encoding="utf-8")
    source_hashes = {}
    with zipfile.ZipFile(artifacts[0]) as project_wheel:
        for path in sorted((repo / "src").rglob("*.py")):
            raw = path.read_bytes()
            if project_wheel.read(path.relative_to(repo / "src").as_posix()) != raw:
                raise SystemExit(f"Source changed during build; rebuild the package: {path}")
            source_hashes[path.relative_to(repo).as_posix()] = hashlib.sha256(raw).hexdigest()
    source_hashes["pyproject.toml"] = hashlib.sha256((repo / "pyproject.toml").read_bytes()).hexdigest()
    (bundle / "SOURCE_SHA256SUMS.json").write_text(json.dumps(source_hashes, indent=2), encoding="utf-8")
    checksums = {p.relative_to(bundle).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(bundle.rglob("*")) if p.is_file()}
    (bundle / "SHA256SUMS.json").write_text(json.dumps(checksums, indent=2), encoding="utf-8")
    archive = bundle.with_suffix(".zip")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as handle:
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                handle.write(path, path.relative_to(output))
    print(json.dumps({"bundle": str(bundle), "archive": str(archive), "platform": tag, "kind": "python-bootstrap"}, indent=2))


if __name__ == "__main__":
    main()
