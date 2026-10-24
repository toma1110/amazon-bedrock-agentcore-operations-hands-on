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
S03_READER_RUNTIME_FILES = {
    "runtime_app.py": SECTION / "s04_runtime_app.py",
    "s04_gateway_tools.py": SECTION / "s04_gateway_tools.py",
    "s03_invocation_reader.py": SECTION / "s03_invocation_reader.py",
    "agent_app.py": S02 / "agent_app.py",
    "runbooks.json": S02 / "runbooks.json",
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


def build_s03_reader_candidate(output_directory: Path, run_id: str) -> tuple[Path, Path]:
    """Build a new, isolated two-artifact package; never overwrite an earlier run."""
    if not run_id or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for ch in run_id):
        raise ValueError("run_id must contain only ASCII letters, digits, hyphen, or underscore")
    if output_directory.exists():
        raise FileExistsError(f"candidate output already exists; choose a fresh run id: {output_directory}")
    missing = [name for name, source in S03_READER_RUNTIME_FILES.items() if not source.is_file()]
    if missing:
        raise FileNotFoundError(f"S04 reader Runtime sources missing: {', '.join(missing)}")
    output_directory.parent.mkdir(parents=True, exist_ok=True)
    output_directory.mkdir()
    runtime_tmp = output_directory / "runtime-candidate.zip"
    lambda_tmp = output_directory / "reader-candidate.zip"
    with tempfile.TemporaryDirectory(prefix="package-", dir=output_directory) as staging_name:
        staging = Path(staging_name)
        requirements = staging / "requirements.txt"
        package = staging / "runtime"
        subprocess.run([
            "uv", "export", "--project", str(S02), "--locked", "--no-dev",
            "--format", "requirements-txt", "--output-file", str(requirements),
        ], check=True, stdout=subprocess.DEVNULL)
        subprocess.run([
            "uv", "pip", "install", "--target", str(package), "--python-version", "3.13",
            "--python-platform", "aarch64-manylinux2014", "--only-binary", ":all:",
            "--requirements", str(requirements),
        ], check=True, stdout=subprocess.DEVNULL)
        for name, source in S03_READER_RUNTIME_FILES.items():
            shutil.copy2(source, package / name)
        _zip_tree(package, runtime_tmp)
        with zipfile.ZipFile(lambda_tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            archive.write(SECTION / "s03_invocation_reader.py", "s03_invocation_reader.py")
        runtime_hash = _sha256(runtime_tmp)
        lambda_hash = _sha256(lambda_tmp)
        # A digest prefix in the filename avoids Windows MAX_PATH; full SHA-256
        # values are printed below and are the identity recorded by operators.
        runtime_zip = output_directory / f"runtime-{runtime_hash[:16]}.zip"
        lambda_zip = output_directory / f"lambda-{lambda_hash[:16]}.zip"
        runtime_tmp.replace(runtime_zip)
        lambda_tmp.replace(lambda_zip)
        with zipfile.ZipFile(runtime_zip) as archive:
            names = set(archive.namelist())
            if not set(S03_READER_RUNTIME_FILES).issubset(names):
                raise RuntimeError("Runtime archive is missing an S04 reader source file")
            if "runtime_app.py" not in names:
                raise RuntimeError("Runtime archive must use the S04-only runtime_app.py entrypoint")
        with zipfile.ZipFile(lambda_zip) as archive:
            if archive.namelist() != ["s03_invocation_reader.py"]:
                raise RuntimeError("reader Lambda archive must contain only its S04 reader module")
        final_lambda = lambda_zip
        final_runtime = runtime_zip
    for path in (final_lambda, final_runtime):
        print(f"{path}  bytes={path.stat().st_size}  sha256={_sha256(path)}")
    return final_lambda, final_runtime


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=SECTION / "build", help="directory for ignored ZIP files")
    parser.add_argument("--profile", choices=["synthetic", "s03-reader"], default="synthetic")
    parser.add_argument("--run-id", help="fresh output directory suffix required for s03-reader candidate")
    args = parser.parse_args()
    if args.profile == "s03-reader":
        if not args.run_id:
            parser.error("--profile s03-reader requires --run-id")
        build_s03_reader_candidate(args.output / f"s03-reader-{args.run_id}", args.run_id)
    else:
        build(args.output)


if __name__ == "__main__":
    main()
