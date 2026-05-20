from __future__ import annotations

import argparse
import base64
import configparser
import csv
import hashlib
import re
import stat
import sys
import tarfile
import tomllib
import zipfile
from email.parser import Parser
from pathlib import Path
from typing import Sequence

BLOCKED_ROOTS = (".github", "build", "dist", "docs", "examples", "scripts", "tests")
REQUIRED_PACKAGE_FILES = (
    "infra/__init__.py",
    "infra/cli.py",
    "infra/scaffold.py",
    "infra/provider_tests/test_live_providers.py",
)
REQUIRED_SDIST_FILES = ("LICENSE", "MANIFEST.in", "README.md", "pyproject.toml")
REQUIRED_SDIST_REGULAR_FILES = (*REQUIRED_SDIST_FILES, "PKG-INFO")
REQUIRED_WHEEL_DIST_INFO_SUFFIXES = (
    ".dist-info/METADATA",
    ".dist-info/WHEEL",
    ".dist-info/entry_points.txt",
    ".dist-info/top_level.txt",
    ".dist-info/RECORD",
)
REQUIRED_WHEEL_LICENSE_SUFFIXES = (".dist-info/licenses/LICENSE",)
REQUIRED_CONSOLE_SCRIPT = ("fastapi-infra", "infra.cli:main")
REQUIRED_TOP_LEVEL_PACKAGE = "infra"
WHEEL_NAME_RE = re.compile(
    r"^(?P<name>[^-]+)-(?P<version>[^-]+)(?:-[^-]+)?-[^-]+-[^-]+-[^-]+\.whl$"
)


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    artifacts = tuple(args.artifacts or sorted(Path("dist").glob("*")))
    if not artifacts:
        print("distribution check: no artifacts found", file=sys.stderr)
        return 1

    artifact_paths = tuple(Path(artifact) for artifact in artifacts)
    errors = _check_artifact_set(artifact_paths)
    for artifact_path in artifact_paths:
        errors.extend(_check_artifact(artifact_path))
    if errors:
        print("distribution check: failed", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print("distribution check: valid")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check_distribution.py",
        description="Validate built sdist/wheel contents before publishing.",
    )
    parser.add_argument(
        "artifacts",
        nargs="*",
        type=Path,
        help="distribution artifacts to inspect; defaults to dist/*",
    )
    return parser


def _check_artifact(artifact: Path) -> list[str]:
    if not artifact.exists():
        return [f"artifact not found: {artifact}"]
    try:
        names = _artifact_names(artifact)
    except (OSError, tarfile.TarError, ValueError, zipfile.BadZipFile) as exc:
        return [f"{artifact}: {exc}"]
    normalized_names = _normalize_artifact_names(names, strip_root=artifact.suffix != ".whl")
    errors = _check_duplicate_archive_names(
        artifact,
        names,
        strip_root=artifact.suffix != ".whl",
    )
    errors.extend(
        _check_unsafe_archive_names(
            artifact,
            names,
            strip_root=artifact.suffix != ".whl",
        )
    )
    errors.extend(
        f"{artifact}: contains blocked path {name}"
        for name in normalized_names
        if _is_blocked_path(name)
    )
    errors.extend(
        f"{artifact}: missing required package file {required}"
        for required in REQUIRED_PACKAGE_FILES
        if required not in normalized_names
    )
    if artifact.suffix == ".whl":
        errors.extend(_check_wheel_required_regular_files(artifact, REQUIRED_PACKAGE_FILES))
        errors.extend(_check_wheel_metadata(artifact, names, normalized_names))
    else:
        errors.extend(_check_sdist_required_regular_files(artifact, REQUIRED_PACKAGE_FILES))
        errors.extend(_check_sdist_metadata(artifact, names, normalized_names))
    return errors


def _check_artifact_set(artifacts: Sequence[Path]) -> list[str]:
    wheels = [artifact for artifact in artifacts if _artifact_kind(artifact) == "wheel"]
    sdists = [artifact for artifact in artifacts if _artifact_kind(artifact) == "sdist"]
    errors: list[str] = []
    if len(wheels) != 1:
        errors.append(f"expected exactly one wheel artifact (*.whl); found {len(wheels)}")
    if len(sdists) != 1:
        errors.append(
            "expected exactly one source distribution artifact (*.tar.gz or *.tgz); "
            f"found {len(sdists)}"
        )
    if len(wheels) == 1 and len(sdists) == 1:
        wheel_identity = _artifact_identity(wheels[0])
        sdist_identity = _artifact_identity(sdists[0])
        if wheel_identity is None:
            errors.append(f"{wheels[0]}: invalid wheel artifact filename")
        if sdist_identity is None:
            errors.append(f"{sdists[0]}: invalid source distribution artifact filename")
        if (
            wheel_identity is not None
            and sdist_identity is not None
            and wheel_identity != sdist_identity
        ):
            errors.append(
                "artifact name/version mismatch: "
                f"wheel {wheel_identity[0]} {wheel_identity[1]}, "
                f"sdist {sdist_identity[0]} {sdist_identity[1]}"
            )
    return errors


def _artifact_kind(artifact: Path) -> str | None:
    if artifact.suffix == ".whl":
        return "wheel"
    if artifact.name.endswith((".tar.gz", ".tgz")):
        return "sdist"
    return None


def _artifact_identity(artifact: Path) -> tuple[str, str] | None:
    if artifact.suffix == ".whl":
        match = WHEEL_NAME_RE.match(artifact.name)
        if match is None:
            return None
        return (_normalize_distribution_name(match.group("name")), match.group("version"))
    if artifact.name.endswith((".tar.gz", ".tgz")):
        source_name = artifact.name.removesuffix(".tar.gz").removesuffix(".tgz")
        if "-" not in source_name:
            return None
        name, version = source_name.rsplit("-", 1)
        if not name or not version:
            return None
        return (_normalize_distribution_name(name), version)
    return None


def _normalize_distribution_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _artifact_names(artifact: Path) -> list[str]:
    if artifact.suffix == ".whl":
        with zipfile.ZipFile(artifact) as wheel:
            return wheel.namelist()
    if artifact.name.endswith((".tar.gz", ".tgz")):
        with tarfile.open(artifact) as source:
            return source.getnames()
    raise ValueError(f"unsupported distribution artifact: {artifact}")


def _artifact_text(artifact: Path, name: str) -> str:
    return _artifact_bytes(artifact, name).decode("utf-8")


def _artifact_bytes(artifact: Path, name: str) -> bytes:
    if artifact.suffix == ".whl":
        with zipfile.ZipFile(artifact) as wheel:
            return wheel.read(name)
    if artifact.name.endswith((".tar.gz", ".tgz")):
        with tarfile.open(artifact) as source:
            member = source.extractfile(name)
            if member is None:
                raise ValueError(f"missing archive member: {name}")
            with member:
                return member.read()
    raise ValueError(f"unsupported distribution artifact: {artifact}")


def _normalize_artifact_names(names: list[str], *, strip_root: bool) -> set[str]:
    normalized: set[str] = set()
    for name in names:
        path = _normalize_archive_name(name, strip_root=strip_root)
        if path:
            normalized.add(path)
    return normalized


def _check_duplicate_archive_names(
    artifact: Path, names: list[str], *, strip_root: bool
) -> list[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for name in names:
        normalized_name = _normalize_archive_name(name, strip_root=strip_root)
        if not normalized_name:
            continue
        if normalized_name in seen:
            duplicates.add(normalized_name)
        seen.add(normalized_name)
    return [f"{artifact}: duplicate archive entry {name}" for name in sorted(duplicates)]


def _check_unsafe_archive_names(artifact: Path, names: list[str], *, strip_root: bool) -> list[str]:
    unsafe_names = {
        _normalize_archive_name(name, strip_root=strip_root)
        for name in names
        if _is_unsafe_archive_name(name, strip_root=strip_root)
    }
    return [f"{artifact}: unsafe archive entry {name}" for name in sorted(unsafe_names) if name]


def _is_unsafe_archive_name(name: str, *, strip_root: bool) -> bool:
    if name.startswith("/"):
        return True
    normalized_name = _normalize_archive_name(name, strip_root=strip_root)
    if "\\" in normalized_name:
        return True
    return any(part in {"", "..", "."} for part in normalized_name.split("/"))


def _normalize_archive_name(name: str, *, strip_root: bool) -> str:
    path = name.strip("/")
    if strip_root and "/" in path:
        return path.split("/", 1)[1]
    if strip_root:
        return ""
    return path


def _is_blocked_path(name: str) -> bool:
    return any(name == root or name.startswith(f"{root}/") for root in BLOCKED_ROOTS)


def _check_wheel_metadata(
    artifact: Path,
    archive_names: list[str],
    normalized_names: set[str],
) -> list[str]:
    errors = [
        f"{artifact}: missing wheel metadata suffix {suffix}"
        for suffix in REQUIRED_WHEEL_DIST_INFO_SUFFIXES
        if not any(_is_wheel_dist_info_file_with_suffix(name, suffix) for name in normalized_names)
    ]
    errors.extend(_check_wheel_required_metadata_regular_files(artifact))
    errors.extend(_check_wheel_required_license_files(artifact, normalized_names))
    errors.extend(_check_wheel_member_types(artifact))
    errors.extend(_check_wheel_dist_info_directory(artifact, archive_names))
    metadata_name = _single_archive_name_with_suffix(archive_names, ".dist-info/METADATA")
    if metadata_name is not None and _wheel_has_regular_suffix(artifact, ".dist-info/METADATA"):
        errors.extend(
            _check_internal_metadata_identity(
                artifact,
                metadata_name,
                label="METADATA",
                mismatch_prefix="wheel metadata mismatch",
            )
        )
    wheel_name = _single_archive_name_with_suffix(archive_names, ".dist-info/WHEEL")
    if wheel_name is not None and _wheel_has_regular_suffix(artifact, ".dist-info/WHEEL"):
        errors.extend(_check_wheel_tags(artifact, wheel_name))
    entry_points_name = _single_archive_name_with_suffix(
        archive_names, ".dist-info/entry_points.txt"
    )
    if entry_points_name is not None and _wheel_has_regular_suffix(
        artifact, ".dist-info/entry_points.txt"
    ):
        errors.extend(_check_wheel_entry_points(artifact, entry_points_name))
    top_level_name = _single_archive_name_with_suffix(archive_names, ".dist-info/top_level.txt")
    if top_level_name is not None and _wheel_has_regular_suffix(
        artifact, ".dist-info/top_level.txt"
    ):
        errors.extend(_check_wheel_top_level(artifact, top_level_name))
    record_name = _single_archive_name_with_suffix(archive_names, ".dist-info/RECORD")
    if record_name is not None and _wheel_has_regular_suffix(artifact, ".dist-info/RECORD"):
        errors.extend(_check_wheel_record(artifact, record_name, archive_names))
    return errors


def _check_wheel_member_types(artifact: Path) -> list[str]:
    errors: list[str] = []
    with zipfile.ZipFile(artifact) as wheel:
        for member in wheel.infolist():
            if member.filename.endswith("/"):
                continue
            name = member.filename.strip("/")
            if not name:
                continue
            unix_mode = member.external_attr >> 16
            file_type = stat.S_IFMT(unix_mode)
            if file_type in {0, stat.S_IFREG}:
                continue
            errors.append(f"{artifact}: wheel contains non-regular file {name}")
    return errors


def _check_wheel_required_regular_files(artifact: Path, required_files: Sequence[str]) -> list[str]:
    member_types: dict[str, set[str]] = {}
    with zipfile.ZipFile(artifact) as wheel:
        for member in wheel.infolist():
            name = _normalize_archive_name(member.filename, strip_root=False)
            if not name:
                continue
            member_types.setdefault(name, set()).add(_wheel_member_type(member))
    return _check_required_regular_file_types(
        artifact, required_files, member_types, label="package"
    )


def _check_wheel_required_metadata_regular_files(artifact: Path) -> list[str]:
    errors: list[str] = []
    with zipfile.ZipFile(artifact) as wheel:
        members = wheel.infolist()
    for suffix in REQUIRED_WHEEL_DIST_INFO_SUFFIXES:
        matches = [
            member
            for member in members
            if _is_wheel_dist_info_file_with_suffix(member.filename, suffix)
        ]
        if matches and not any(_wheel_member_type(member) == "file" for member in matches):
            errors.append(
                f"{artifact}: required wheel metadata file is not a regular file "
                f"{suffix.rsplit('/', 1)[1]}"
            )
    return errors


def _check_wheel_required_license_files(artifact: Path, normalized_names: set[str]) -> list[str]:
    return [
        f"{artifact}: missing required wheel license file {suffix.rsplit('/', 1)[1]}"
        for suffix in REQUIRED_WHEEL_LICENSE_SUFFIXES
        if not any(_is_wheel_dist_info_member_with_suffix(name, suffix) for name in normalized_names)
    ]


def _wheel_has_regular_suffix(artifact: Path, suffix: str) -> bool:
    with zipfile.ZipFile(artifact) as wheel:
        return any(
            _is_wheel_dist_info_file_with_suffix(member.filename, suffix)
            and _wheel_member_type(member) == "file"
            for member in wheel.infolist()
        )


def _wheel_member_type(member: zipfile.ZipInfo) -> str:
    name = member.filename.strip("/")
    if member.filename.endswith("/"):
        return "directory"
    unix_mode = member.external_attr >> 16
    file_type = stat.S_IFMT(unix_mode)
    if file_type in {0, stat.S_IFREG}:
        return "file"
    return "other"


def _check_sdist_metadata(
    artifact: Path,
    archive_names: list[str],
    normalized_names: set[str],
) -> list[str]:
    errors = _check_sdist_root_directory(artifact, archive_names)
    errors.extend(_check_sdist_member_types(artifact))
    errors.extend(
        _check_sdist_required_source_regular_files(artifact, REQUIRED_SDIST_REGULAR_FILES)
    )
    errors.extend(
        f"{artifact}: missing required source file {required}"
        for required in REQUIRED_SDIST_FILES
        if required not in normalized_names
    )
    pkg_info_name = _single_normalized_archive_name(archive_names, "PKG-INFO")
    if pkg_info_name is None:
        errors.append(f"{artifact}: missing required source metadata PKG-INFO")
    elif _sdist_has_regular_file(artifact, "PKG-INFO"):
        errors.extend(
            _check_internal_metadata_identity(
                artifact,
                pkg_info_name,
                label="PKG-INFO",
                mismatch_prefix="sdist metadata mismatch",
            )
        )
    pyproject_name = _single_normalized_archive_name(archive_names, "pyproject.toml")
    if pyproject_name is not None and _sdist_has_regular_file(artifact, "pyproject.toml"):
        errors.extend(_check_sdist_pyproject(artifact, pyproject_name))
    return errors


def _check_sdist_required_regular_files(artifact: Path, required_files: Sequence[str]) -> list[str]:
    member_types: dict[str, set[str]] = {}
    with tarfile.open(artifact) as source:
        for member in source.getmembers():
            name = _normalize_archive_name(member.name, strip_root=True)
            if not name:
                continue
            member_types.setdefault(name, set()).add(_sdist_member_type(member))
    return _check_required_regular_file_types(
        artifact, required_files, member_types, label="package"
    )


def _check_sdist_required_source_regular_files(
    artifact: Path, required_files: Sequence[str]
) -> list[str]:
    member_types: dict[str, set[str]] = {}
    with tarfile.open(artifact) as source:
        for member in source.getmembers():
            name = _normalize_archive_name(member.name, strip_root=True)
            if not name:
                continue
            member_types.setdefault(name, set()).add(_sdist_member_type(member))
    return _check_required_regular_file_types(
        artifact, required_files, member_types, label="source"
    )


def _sdist_has_regular_file(artifact: Path, normalized_name: str) -> bool:
    with tarfile.open(artifact) as source:
        for member in source.getmembers():
            name = _normalize_archive_name(member.name, strip_root=True)
            if name == normalized_name and member.isfile():
                return True
    return False


def _sdist_member_type(member: tarfile.TarInfo) -> str:
    if member.isfile():
        return "file"
    if member.isdir():
        return "directory"
    return "other"


def _check_required_regular_file_types(
    artifact: Path,
    required_files: Sequence[str],
    member_types: dict[str, set[str]],
    *,
    label: str,
) -> list[str]:
    errors: list[str] = []
    for required in required_files:
        types = member_types.get(required, set())
        if types and "file" not in types:
            errors.append(f"{artifact}: required {label} file is not a regular file {required}")
    return errors


def _check_sdist_member_types(artifact: Path) -> list[str]:
    expected_root = artifact.name.removesuffix(".tar.gz").removesuffix(".tgz")
    errors: list[str] = []
    with tarfile.open(artifact) as source:
        for member in source.getmembers():
            stripped_name = member.name.strip("/")
            if stripped_name == expected_root:
                if not member.isdir():
                    errors.append(
                        f"{artifact}: source distribution root is not a directory "
                        f"{expected_root}"
                    )
                continue
            normalized_name = _normalize_archive_name(member.name, strip_root=True)
            if not normalized_name:
                continue
            if member.isfile() or member.isdir():
                continue
            errors.append(
                f"{artifact}: source distribution contains non-regular file " f"{normalized_name}"
            )
    return errors


def _check_sdist_root_directory(artifact: Path, archive_names: list[str]) -> list[str]:
    expected_root = artifact.name.removesuffix(".tar.gz").removesuffix(".tgz")
    outside_root = []
    roots = {
        name.strip("/").split("/", 1)[0]
        for name in archive_names
        if name.strip("/") and "/" in name.strip("/")
    }
    for name in archive_names:
        stripped = name.strip("/")
        if stripped and stripped != expected_root and not stripped.startswith(f"{expected_root}/"):
            outside_root.append(stripped)
    errors = [
        f"{artifact}: sdist member outside root directory {expected_root}: {name}"
        for name in sorted(outside_root)
    ]
    if roots == {expected_root} and not errors:
        return errors
    if not roots:
        errors.append(f"{artifact}: missing sdist root directory {expected_root}")
        return errors
    if roots != {expected_root}:
        errors.append(
            f"{artifact}: sdist root directory mismatch: expected {expected_root}, "
            f"found {', '.join(sorted(roots))}"
        )
    return errors


def _check_internal_metadata_identity(
    artifact: Path,
    metadata_name: str,
    *,
    label: str,
    mismatch_prefix: str,
) -> list[str]:
    artifact_identity = _artifact_identity(artifact)
    if artifact_identity is None:
        return []
    metadata_identity = _metadata_identity(_artifact_text(artifact, metadata_name))
    if metadata_identity is None:
        return [f"{artifact}: {label} missing Name or Version"]
    if metadata_identity != artifact_identity:
        return [
            f"{artifact}: {mismatch_prefix}: filename "
            f"{artifact_identity[0]} {artifact_identity[1]}, "
            f"{label} {metadata_identity[0]} {metadata_identity[1]}"
        ]
    return []


def _metadata_identity(text: str) -> tuple[str, str] | None:
    metadata = Parser().parsestr(text)
    name = metadata.get("Name")
    version = metadata.get("Version")
    if not name or not version:
        return None
    return (_normalize_distribution_name(name), version)


def _check_wheel_dist_info_directory(artifact: Path, archive_names: list[str]) -> list[str]:
    directories = _wheel_dist_info_directories(archive_names)
    if not directories:
        return []
    if len(directories) != 1:
        return [
            f"{artifact}: expected exactly one wheel dist-info directory; found {len(directories)}"
        ]
    artifact_identity = _artifact_identity(artifact)
    if artifact_identity is None:
        return []
    directory = next(iter(directories))
    dist_info_identity = _wheel_dist_info_identity(directory)
    if dist_info_identity is None:
        return [f"{artifact}: invalid wheel dist-info directory {directory}"]
    if dist_info_identity != artifact_identity:
        return [
            f"{artifact}: wheel dist-info directory mismatch: filename "
            f"{artifact_identity[0]} {artifact_identity[1]}, "
            f"dist-info {dist_info_identity[0]} {dist_info_identity[1]}"
        ]
    return []


def _wheel_dist_info_directories(archive_names: list[str]) -> set[str]:
    directories: set[str] = set()
    for name in archive_names:
        directory = name.strip("/").split("/", 1)[0]
        if directory.endswith(".dist-info"):
            directories.add(directory)
    return directories


def _wheel_dist_info_identity(directory: str) -> tuple[str, str] | None:
    stem = directory.removesuffix(".dist-info")
    if "-" not in stem:
        return None
    name, version = stem.rsplit("-", 1)
    if not name or not version:
        return None
    return (_normalize_distribution_name(name), version)


def _check_wheel_tags(artifact: Path, wheel_name: str) -> list[str]:
    filename_tag = _wheel_filename_tag(artifact)
    if filename_tag is None:
        return []
    metadata = Parser().parsestr(_artifact_text(artifact, wheel_name))
    wheel_tags = metadata.get_all("Tag") or []
    if filename_tag not in wheel_tags:
        wheel_tag_text = ", ".join(wheel_tags) if wheel_tags else "<missing>"
        return [f"{artifact}: wheel tag mismatch: filename {filename_tag}, WHEEL {wheel_tag_text}"]
    return []


def _wheel_filename_tag(artifact: Path) -> str | None:
    if artifact.suffix != ".whl":
        return None
    parts = artifact.name.removesuffix(".whl").rsplit("-", 3)
    if len(parts) != 4:
        return None
    return "-".join(parts[1:])


def _check_wheel_entry_points(artifact: Path, entry_points_name: str) -> list[str]:
    parser = configparser.ConfigParser()
    try:
        parser.read_string(_artifact_text(artifact, entry_points_name))
    except configparser.Error as exc:
        return [f"{artifact}: invalid wheel entry_points.txt: {exc}"]
    script_name, script_target = REQUIRED_CONSOLE_SCRIPT
    if not parser.has_section("console_scripts"):
        return [f"{artifact}: missing console script entry point {script_name} = {script_target}"]
    if parser.get("console_scripts", script_name, fallback=None) != script_target:
        return [f"{artifact}: missing console script entry point {script_name} = {script_target}"]
    return []


def _check_wheel_top_level(artifact: Path, top_level_name: str) -> list[str]:
    top_level_packages = {
        line.strip()
        for line in _artifact_text(artifact, top_level_name).splitlines()
        if line.strip()
    }
    if REQUIRED_TOP_LEVEL_PACKAGE not in top_level_packages:
        return [
            f"{artifact}: wheel top_level.txt missing required top-level "
            f"package {REQUIRED_TOP_LEVEL_PACKAGE}"
        ]
    return []


def _check_wheel_record(artifact: Path, record_name: str, archive_names: list[str]) -> list[str]:
    record_rows, record_errors = _wheel_record_rows(_artifact_text(artifact, record_name))
    record_paths = set(record_rows)
    archive_paths = _wheel_archive_file_paths(archive_names)
    errors = [f"{artifact}: {error}" for error in record_errors]
    for archive_name in sorted(archive_paths):
        if archive_name not in record_paths:
            errors.append(f"{artifact}: wheel RECORD missing archive entry {archive_name}")
    for record_path in sorted(record_paths):
        if record_path not in archive_paths:
            errors.append(
                f"{artifact}: wheel RECORD references missing archive entry {record_path}"
            )
            continue
        errors.extend(
            _check_wheel_record_digest(
                artifact,
                record_path,
                record_rows[record_path],
                record_name=record_name,
            )
        )
    return errors


def _wheel_archive_file_paths(archive_names: list[str]) -> set[str]:
    paths: set[str] = set()
    for name in archive_names:
        if name.endswith("/"):
            continue
        archive_name = name.strip("/")
        if archive_name:
            paths.add(archive_name)
    return paths


def _wheel_record_rows(record_content: str) -> tuple[dict[str, tuple[str, str]], list[str]]:
    rows: dict[str, tuple[str, str]] = {}
    errors: list[str] = []
    for row in csv.reader(record_content.splitlines()):
        if not row:
            continue
        path = row[0]
        if len(row) != 3:
            row_label = path or "<empty>"
            errors.append(f"invalid wheel RECORD row for {row_label}: expected 3 columns")
            continue
        if not path:
            errors.append("invalid wheel RECORD row for <empty>: path is required")
            continue
        if _is_unsafe_archive_name(path, strip_root=False):
            errors.append(f"unsafe wheel RECORD entry {path}")
            continue
        if path.endswith("/"):
            errors.append(f"wheel RECORD entry {path} must not reference a directory")
            continue
        if path in rows:
            errors.append(f"duplicate wheel RECORD entry {path}")
            continue
        rows[path] = (row[1], row[2])
    return rows, errors


def _check_wheel_record_digest(
    artifact: Path,
    record_path: str,
    record_digest: tuple[str, str],
    *,
    record_name: str,
) -> list[str]:
    digest, size = record_digest
    if record_path == record_name:
        if digest or size:
            return [f"{artifact}: wheel RECORD entry {record_path} must not include hash or size"]
        return []
    if not digest or not size:
        return [f"{artifact}: wheel RECORD entry {record_path} missing hash or size"]
    content = _artifact_bytes(artifact, record_path)
    expected_digest = _record_hash(content)
    expected_size = str(len(content))
    errors: list[str] = []
    if digest != expected_digest:
        errors.append(f"{artifact}: wheel RECORD hash mismatch for {record_path}")
    if size != expected_size:
        errors.append(
            f"{artifact}: wheel RECORD size mismatch for {record_path}: "
            f"expected {expected_size}, found {size}"
        )
    return errors


def _record_hash(content: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode("ascii")
    return f"sha256={digest.rstrip('=')}"


def _check_sdist_pyproject(artifact: Path, pyproject_name: str) -> list[str]:
    try:
        pyproject = tomllib.loads(_artifact_text(artifact, pyproject_name))
    except tomllib.TOMLDecodeError as exc:
        return [f"{artifact}: invalid sdist pyproject.toml: {exc}"]
    errors: list[str] = []
    script_name, script_target = REQUIRED_CONSOLE_SCRIPT
    artifact_identity = _artifact_identity(artifact)
    project = pyproject.get("project")
    pyproject_identity = _pyproject_identity(project) if isinstance(project, dict) else None
    if pyproject_identity is None:
        errors.append(f"{artifact}: sdist pyproject.toml missing project name or version")
    elif artifact_identity is not None and pyproject_identity != artifact_identity:
        errors.append(
            f"{artifact}: sdist pyproject.toml metadata mismatch: filename "
            f"{artifact_identity[0]} {artifact_identity[1]}, "
            f"pyproject {pyproject_identity[0]} {pyproject_identity[1]}"
        )
    scripts = project.get("scripts") if isinstance(project, dict) else None
    if not isinstance(scripts, dict) or scripts.get(script_name) != script_target:
        errors.append(
            f"{artifact}: sdist pyproject.toml missing project.scripts."
            f"{script_name} = {script_target}"
        )
    return errors


def _pyproject_identity(project: dict[str, object]) -> tuple[str, str] | None:
    name = project.get("name")
    version = project.get("version")
    if not isinstance(name, str) or not isinstance(version, str):
        return None
    return (_normalize_distribution_name(name), version)


def _single_archive_name_with_suffix(names: list[str], suffix: str) -> str | None:
    matches = [name for name in names if _is_wheel_dist_info_file_with_suffix(name, suffix)]
    if len(matches) != 1:
        return None
    return matches[0]


def _is_wheel_dist_info_file_with_suffix(name: str, suffix: str) -> bool:
    stripped = name.strip("/")
    if "/" not in stripped:
        return False
    directory, filename = stripped.split("/", 1)
    return "/" not in filename and f"{directory}/{filename}".endswith(suffix)


def _is_wheel_dist_info_member_with_suffix(name: str, suffix: str) -> bool:
    stripped = name.strip("/")
    if "/" not in stripped:
        return False
    directory = stripped.split("/", 1)[0]
    return directory.endswith(".dist-info") and stripped.endswith(suffix)


def _single_normalized_archive_name(names: list[str], normalized_name: str) -> str | None:
    matches = []
    for name in names:
        stripped = name.strip("/")
        if "/" in stripped:
            candidate = stripped.split("/", 1)[1]
        else:
            candidate = stripped
        if candidate == normalized_name:
            matches.append(name)
    if len(matches) != 1:
        return None
    return matches[0]


if __name__ == "__main__":
    raise SystemExit(main())
