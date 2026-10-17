"""Build the S07 Runtime ZIP from the locked S02 dependency set."""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import zipfile
from pathlib import Path

SECTION = Path(__file__).resolve().parent
REPOSITORY = SECTION.parents[1]
S02 = REPOSITORY / "sections" / "s02"
S03 = REPOSITORY / "sections" / "s03"
S06 = REPOSITORY / "sections" / "s06"
RUNTIME_SOURCE_FILES = {
    "agent_app.py": S02 / "agent_app.py",
    "runbooks.json": S02 / "runbooks.json",
    "gateway_tools.py": S03 / "gateway_tools.py",
    "runtime_app.py": SECTION / "runtime_app.py",
    "fault_injection.py": SECTION / "fault_injection.py",
    "runtime_observability.py": S06 / "runtime_observability.py",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build(output_directory: Path) -> Path:
    output_directory = output_directory.resolve()
    if output_directory.exists():
        raise FileExistsError(f"Output path already exists; choose a new run directory: {output_directory}")
    output_directory.mkdir(parents=True, exist_ok=False)
    archive_path = output_directory / "s07-runtime-arm64.zip"
    requirements = output_directory / "requirements.txt"
    package = output_directory / "runtime"
    subprocess.run([
        "uv", "export", "--project", str(S02), "--locked", "--no-dev",
        "--format", "requirements-txt", "--output-file", str(requirements),
    ], check=True, stdout=subprocess.DEVNULL)
    subprocess.run([
        "uv", "pip", "install", "--target", str(package), "--python-version", "3.13",
        "--python-platform", "aarch64-manylinux2014", "--only-binary", ":all:",
        "--requirements", str(requirements),
    ], check=True, stdout=subprocess.DEVNULL)
    missing = [name for name, source in RUNTIME_SOURCE_FILES.items() if not source.is_file()]
    if missing:
        raise FileNotFoundError(f"Runtime source files are missing: {', '.join(missing)}")
    for name, source in RUNTIME_SOURCE_FILES.items():
        shutil.copy2(source, package / name)
    with zipfile.ZipFile(archive_path, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for file_path in sorted(package.rglob("*"), key=lambda item: item.as_posix().lower()):
            if not file_path.is_file():
                continue
            if any(part in {"__pycache__", ".pytest_cache", "tests"} for part in file_path.parts):
                continue
            if file_path.name.endswith((".pyc", ".pyo")):
                continue
            archive.write(file_path, file_path.relative_to(package).as_posix())
    with zipfile.ZipFile(archive_path) as archive:
        missing = set(RUNTIME_SOURCE_FILES) - set(archive.namelist())
        if missing:
            raise RuntimeError(f"Runtime archive is missing: {', '.join(sorted(missing))}")
    print(f"{archive_path} bytes={archive_path.stat().st_size} sha256={_sha256(archive_path)}")
    return archive_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="new, unique ignored output directory for this build")
    build(parser.parse_args().output)


if __name__ == "__main__":
    main()
