"""Build the S04 Lambda and ARM64 AgentCore Runtime ZIP artifacts."""

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
RUNTIME_SOURCE_FILES = {
    "agent_app.py": S02 / "agent_app.py",
    "runbooks.json": S02 / "runbooks.json",
    "runtime_app.py": S03 / "runtime_app.py",
    "gateway_tools.py": S03 / "gateway_tools.py",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _zip_tree(source: Path, destination: Path, *, extra_files: tuple[Path, ...] = ()) -> None:
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        files = [p for p in source.rglob("*") if p.is_file()]
        files.extend(extra_files)
        for file_path in sorted(set(files), key=lambda item: item.as_posix().lower()):
            if any(part in {"__pycache__", ".pytest_cache", "tests"} for part in file_path.parts):
                continue
            if file_path.name.endswith((".pyc", ".pyo")):
                continue
            arcname = file_path.relative_to(source).as_posix() if file_path.is_relative_to(source) else file_path.name
            archive.write(file_path, arcname)


def build(output_directory: Path) -> tuple[Path, Path]:
    output_directory.mkdir(parents=True, exist_ok=True)
    lambda_zip = output_directory / "s04-lambda.zip"
    runtime_zip = output_directory / "s04-runtime-arm64.zip"

    with zipfile.ZipFile(lambda_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.write(SECTION / "cloudwatch_reader.py", "cloudwatch_reader.py")

    with tempfile.TemporaryDirectory(prefix="s04-runtime-build-") as temporary:
        temp = Path(temporary)
        requirements = temp / "requirements.txt"
        package = temp / "runtime"
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
        _zip_tree(package, runtime_zip)

    with zipfile.ZipFile(lambda_zip) as archive:
        if archive.namelist() != ["cloudwatch_reader.py"]:
            raise RuntimeError("Lambda archive must contain exactly cloudwatch_reader.py")
    with zipfile.ZipFile(runtime_zip) as archive:
        names = set(archive.namelist())
        missing = set(RUNTIME_SOURCE_FILES) - names
        if missing:
            raise RuntimeError(f"Runtime archive is missing: {', '.join(sorted(missing))}")
    for path in (lambda_zip, runtime_zip):
        print(f"{path}  bytes={path.stat().st_size}  sha256={_sha256(path)}")
    return lambda_zip, runtime_zip


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=SECTION / "build", help="directory for ignored ZIP files")
    args = parser.parse_args()
    build(args.output)


if __name__ == "__main__":
    main()
