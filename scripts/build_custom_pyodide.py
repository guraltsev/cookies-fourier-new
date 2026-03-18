#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import shutil
import subprocess
import os
import sys
import tarfile
import urllib.request
from pathlib import Path

PYODIDE_VERSION = "0.29.3"
PYODIDE_URL = (
    f"https://github.com/pyodide/pyodide/releases/download/{PYODIDE_VERSION}/"
    f"pyodide-{PYODIDE_VERSION}.tar.bz2"
)
PYODIDE_SHA256 = "458e8ddbcbb6e21037d3237cd5c5146c451765bc738dfa2249ff34c5140331e4"
PYODIDE_PYTHON_VERSION = "3.13"
DEFAULT_REQUIREMENTS = Path("pyodide-extra-requirements.txt")
REQUIRED_PACKAGES = {
    "numpy",
    "pandas",
    "scipy",
    "sympy",
    "ipywidgets",
    "anywidget",
    "plotly",
}


def sha256sum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_file(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url) as response, destination.open("wb") as handle:
        shutil.copyfileobj(response, handle)


def ensure_archive(cache_dir: Path) -> Path:
    archive = cache_dir / f"pyodide-{PYODIDE_VERSION}.tar.bz2"
    if archive.exists() and sha256sum(archive) == PYODIDE_SHA256:
        return archive
    if archive.exists():
        archive.unlink()
    print(f"Downloading {PYODIDE_URL} -> {archive}")
    download_file(PYODIDE_URL, archive)
    actual = sha256sum(archive)
    if actual != PYODIDE_SHA256:
        archive.unlink(missing_ok=True)
        raise RuntimeError(
            "Pyodide archive checksum mismatch: "
            f"expected {PYODIDE_SHA256}, got {actual}"
        )
    return archive


def flatten_extracted_tree(output_dir: Path) -> None:
    if (output_dir / "pyodide.js").exists():
        return
    entries = [entry for entry in output_dir.iterdir() if entry.name != ".DS_Store"]
    if len(entries) != 1 or not entries[0].is_dir():
        raise RuntimeError(
            "Could not find pyodide.js after extraction and could not flatten the archive "
            f"layout in {output_dir}"
        )
    inner = entries[0]
    if not (inner / "pyodide.js").exists():
        raise RuntimeError(f"Expected {inner / 'pyodide.js'} to exist after extraction")
    for child in inner.iterdir():
        shutil.move(str(child), output_dir / child.name)
    inner.rmdir()


def extract_archive(archive_path: Path, output_dir: Path) -> None:
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"Extracting {archive_path} -> {output_dir}")
    with tarfile.open(archive_path, mode="r:bz2") as archive:
        try:
            archive.extractall(output_dir, filter="data")
        except TypeError:
            archive.extractall(output_dir)
    flatten_extracted_tree(output_dir)
    for required in ["pyodide.js", "pyodide-lock.json"]:
        if not (output_dir / required).exists():
            raise RuntimeError(f"Missing required Pyodide asset: {required}")


def run_checked(cmd: list[str], cwd: Path | None = None) -> None:
    print("Running:", " ".join(cmd))
    try:
        subprocess.run(cmd, cwd=cwd, check=True, text=True)
    except subprocess.CalledProcessError as err:
        location = f" (cwd={cwd})" if cwd else ""
        raise RuntimeError(
            f"Command failed with exit code {err.returncode}{location}: {' '.join(cmd)}"
        ) from err


def run_capture(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    print("Running:", " ".join(cmd))
    return subprocess.run(
        cmd,
        cwd=cwd,
        check=False,
        text=True,
        capture_output=True,
    )


def download_wheels(requirements_file: Path, output_dir: Path) -> list[Path]:
    wheel_dir = output_dir / "wheels"
    if wheel_dir.exists():
        shutil.rmtree(wheel_dir)
    wheel_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "download",
        "--dest",
        str(wheel_dir),
        "--only-binary=:all:",
        "--platform",
        "any",
        "--python-version",
        PYODIDE_PYTHON_VERSION,
        "--implementation",
        "py",
        "--abi",
        "none",
        "--requirement",
        str(requirements_file),
    ]
    run_checked(cmd)
    wheels = sorted(wheel_dir.glob("*.whl"))
    if not wheels:
        raise RuntimeError(f"No wheels were downloaded from {requirements_file}")
    non_portable = [wheel.name for wheel in wheels if not wheel.name.endswith("none-any.whl")]
    if non_portable:
        raise RuntimeError(
            "Only implementation-agnostic, platform-agnostic wheels are supported by "
            "this repo workflow. Unexpected wheels: "
            f"{', '.join(non_portable)}"
        )
    return wheels


def validate_pyodide_cli_environment() -> None:
    try:
        importlib.import_module("wheel.cli.pack")
    except ModuleNotFoundError as err:
        if getattr(err, "name", None) != "wheel.cli":
            raise
        raise RuntimeError(
            "The installed 'wheel' package is too new for pyodide-build "
            f"{PYODIDE_VERSION}. Pyodide 0.29.3 still relies on the public "
            "wheel.cli module, which newer wheel releases removed. "
            "Install a compatible version such as 'wheel<0.46' before running this script."
        ) from err


def find_pyodide_cli() -> str:
    validate_pyodide_cli_environment()
    pyodide_exe = shutil.which("pyodide")
    if not pyodide_exe:
        raise RuntimeError(
            "The 'pyodide' CLI was not found on PATH. Install pyodide-build before running "
            "this script."
        )
    return pyodide_exe


def add_wheels_to_lockfile(output_dir: Path, wheels: list[Path]) -> None:
    input_lock = output_dir / "pyodide-lock.json"
    output_lock = output_dir / "pyodide-lock.updated.json"
    wheel_args = [os.path.relpath(wheel, output_dir) for wheel in wheels]

    def build_cmd(ignore_missing_dependencies: bool) -> list[str]:
        cmd = [
            find_pyodide_cli(),
            "lockfile",
            "add-wheels",
            "--input",
            input_lock.name,
            "--output",
            output_lock.name,
            "--base-path",
            ".",
        ]
        if ignore_missing_dependencies:
            cmd.append("--ignore-missing-dependencies")
        cmd.extend(wheel_args)
        return cmd

    attempts = [
        (False, "strict dependency validation"),
        (True, "allowing the CLI to tolerate dependency-resolution gaps"),
    ]
    last_failure = None
    for ignore_missing, description in attempts:
        result = run_capture(build_cmd(ignore_missing), cwd=output_dir)
        if result.returncode == 0 and output_lock.exists():
            output_lock.replace(input_lock)
            return
        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()
        last_failure = RuntimeError(
            "pyodide lockfile add-wheels failed while "
            f"{description}.\n"
            f"stdout:\n{stdout or '[no stdout]'}\n\n"
            f"stderr:\n{stderr or '[no stderr]'}"
        )

    assert last_failure is not None
    raise last_failure


def validate_lockfile(output_dir: Path) -> None:
    lockfile = output_dir / "pyodide-lock.json"
    data = json.loads(lockfile.read_text(encoding="utf-8"))
    packages = data.get("packages", {})
    missing = sorted(REQUIRED_PACKAGES - set(packages))
    if missing:
        raise RuntimeError(
            "The generated pyodide-lock.json is missing required packages: "
            + ", ".join(missing)
        )
    print("Validated pyodide-lock.json contains:", ", ".join(sorted(REQUIRED_PACKAGES)))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download the official Pyodide distribution, add extra pure-Python wheels to its "
            "lockfile, and write the result into a directory that JupyterLite can serve."
        )
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Directory where the modified Pyodide distribution will be written.",
    )
    parser.add_argument(
        "--requirements-file",
        type=Path,
        default=DEFAULT_REQUIREMENTS,
        help="Pinned requirements file for extra wheels to bundle into Pyodide.",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(".cache") / "pyodide",
        help="Directory used to cache the downloaded Pyodide archive.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    validate_pyodide_cli_environment()
    archive = ensure_archive(args.cache_dir)
    extract_archive(archive, args.output_dir)
    wheels = download_wheels(args.requirements_file, args.output_dir)
    add_wheels_to_lockfile(args.output_dir, wheels)
    validate_lockfile(args.output_dir)
    print(f"Custom Pyodide distribution written to {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
