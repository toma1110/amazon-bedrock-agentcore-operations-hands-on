"""Build the S09 ARM64 AgentCore Runtime archive from the locked S02 dependencies."""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

SECTION = Path(__file__).resolve().parent
REPOSITORY = SECTION.parents[1]
S02 = REPOSITORY / "sections" / "s02"
S03 = REPOSITORY / "sections" / "s03"
SOURCE_FILES = {
    "agent_app.py": S02 / "agent_app.py",
    "runbooks.json": S02 / "runbooks.json",
    "runtime_app.py": SECTION / "runtime_app.py",
    "investigation_agent.py": SECTION / "investigation_agent.py",
    "gateway_tools.py": S03 / "gateway_tools.py",
}
MAX_ARCHIVE_BYTES = 250_000_000


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _zip_tree(source: Path, destination: Path) -> None:
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for file_path in sorted((p for p in source.rglob("*") if p.is_file()), key=lambda item: item.as_posix().lower()):
            if any(part in {"__pycache__", ".pytest_cache", "tests"} for part in file_path.parts):
                continue
            if file_path.suffix in {".pyc", ".pyo"}:
                continue
            archive.write(file_path, file_path.relative_to(source).as_posix())


def build(output_directory: Path) -> Path:
    output_directory.mkdir(parents=True, exist_ok=True)
    missing = [str(source) for source in SOURCE_FILES.values() if not source.is_file()]
    if missing:
        raise FileNotFoundError("Missing runtime source files: " + ", ".join(missing))
    archive_path = output_directory / "s09-runtime-arm64.zip"
    with tempfile.TemporaryDirectory(prefix="s09-runtime-build-") as temporary:
        work = Path(temporary)
        requirements = work / "requirements.txt"
        package = work / "runtime"
        package.mkdir()
        subprocess.run([
            "uv", "export", "--project", str(S02), "--locked", "--no-dev",
            "--format", "requirements-txt", "--output-file", str(requirements),
        ], check=True, stdout=subprocess.DEVNULL)
        subprocess.run([
            "uv", "pip", "install", "--target", str(package), "--python-version", "3.13",
            "--python-platform", "aarch64-manylinux2014", "--only-binary", ":all:",
            "--requirements", str(requirements),
        ], check=True, stdout=subprocess.DEVNULL)
        for name, source in SOURCE_FILES.items():
            shutil.copy2(source, package / name)
        _zip_tree(package, archive_path)

    if archive_path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise RuntimeError("Runtime ZIP exceeds the AgentCore direct-code archive size limit")
    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
    missing_entries = set(SOURCE_FILES) - names
    if missing_entries:
        raise RuntimeError("Runtime ZIP is missing required source: " + ", ".join(sorted(missing_entries)))
    print(f"{archive_path} bytes={archive_path.stat().st_size} sha256={_sha256(archive_path)}")
    return archive_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=SECTION / "build", help="ignored directory for local ZIP output")
    build(parser.parse_args().output)


if __name__ == "__main__":
    main()
