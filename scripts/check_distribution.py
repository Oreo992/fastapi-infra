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
from collections import Counter
from email.parser import Parser
from pathlib import Path
from typing import Sequence, TypeAlias
from urllib.parse import urlsplit

from packaging.licenses import InvalidLicenseExpression, canonicalize_license_expression
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import InvalidName, canonicalize_name
from packaging.version import InvalidVersion, Version

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
SINGLETON_WHEEL_METADATA_FIELDS = ("Root-Is-Purelib", "Wheel-Version")
ALLOWED_SDIST_EGG_INFO_FILES = (
    "PKG-INFO",
    "SOURCES.txt",
    "dependency_links.txt",
    "entry_points.txt",
    "requires.txt",
    "top_level.txt",
)
REQUIRED_CONSOLE_SCRIPT = ("fastapi-infra", "infra.cli:main")
REQUIRED_BUILD_BACKEND = "setuptools.build_meta"
SUPPORTED_BUILD_SYSTEM_FIELDS = ("build-backend", "requires")
REQUIRED_BUILD_SYSTEM_REQUIRES = ("setuptools>=77.0.0", "wheel")
REQUIRED_TOP_LEVEL_PACKAGE = "infra"
REQUIRED_CORE_DEPENDENCIES = (
    "fastapi",
    "uvicorn",
    "starlette",
    "pydantic",
    "pydantic-settings",
    "loguru",
)
SINGLETON_CORE_METADATA_FIELDS = (
    "Author",
    "Author-email",
    "Description-Content-Type",
    "Keywords",
    "License-Expression",
    "Metadata-Version",
    "Name",
    "Requires-Python",
    "Summary",
    "Version",
)
ALLOWED_DYNAMIC_METADATA_FIELDS = ("license-file",)
SUPPORTED_PYPROJECT_AUTHOR_FIELDS = ("email", "name")
UNSUPPORTED_PYPROJECT_FIELDS = ("entry-points", "gui-scripts", "maintainers")
UNSUPPORTED_CORE_METADATA_FIELDS = (
    "Download-URL",
    "Home-page",
    "Maintainer",
    "Maintainer-email",
    "Obsoletes",
    "Obsoletes-Dist",
    "Platform",
    "Provides",
    "Provides-Dist",
    "Requires",
    "Requires-External",
    "Supported-Platform",
)
WHEEL_NAME_RE = re.compile(
    r"^(?P<name>[^-]+)-(?P<version>[^-]+)(?:-[^-]+)?-[^-]+-[^-]+-[^-]+\.whl$"
)
EXTRA_NAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")
RequirementSignature: TypeAlias = tuple[str, tuple[str, ...], tuple[str, ...], str]
CHECK_ERROR_EXCEPTIONS: tuple[type[BaseException], ...] = (
    OSError,
    tarfile.TarError,
    ValueError,
    zipfile.BadZipFile,
    UnicodeDecodeError,
    tomllib.TOMLDecodeError,
    configparser.Error,
)


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    artifacts = tuple(args.artifacts or sorted(Path("dist").glob("*")))
    if not artifacts:
        print("distribution check: no artifacts found", file=sys.stderr)
        return 1

    artifact_paths = tuple(Path(artifact) for artifact in artifacts)
    errors: list[str] = []
    try:
        errors.extend(_check_artifact_set(artifact_paths))
    except CHECK_ERROR_EXCEPTIONS as exc:
        errors.append(str(exc))
    for artifact_path in artifact_paths:
        try:
            errors.extend(_check_artifact(artifact_path))
        except CHECK_ERROR_EXCEPTIONS as exc:
            errors.append(str(exc))
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
        f"{artifact}: contains generated cache file {name}"
        for name in sorted(normalized_names)
        if _is_generated_cache_file(name)
    )
    errors.extend(
        f"{artifact}: contains generated metadata file {name}"
        for name in sorted(normalized_names)
        if _is_generated_metadata_file(name)
    )
    errors.extend(
        f"{artifact}: missing required package file {required}"
        for required in REQUIRED_PACKAGE_FILES
        if required not in normalized_names
    )
    if artifact.suffix == ".whl":
        errors.extend(_check_wheel_required_regular_files(artifact, REQUIRED_PACKAGE_FILES))
        errors.extend(_check_wheel_allowed_top_level_paths(artifact, names))
        errors.extend(_check_wheel_metadata(artifact, names, normalized_names))
    else:
        errors.extend(_check_sdist_required_regular_files(artifact, REQUIRED_PACKAGE_FILES))
        errors.extend(_check_sdist_allowed_top_level_paths(artifact, normalized_names))
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
        errors.extend(_check_wheel_metadata_matches_sdist_pyproject(wheels[0], sdists[0]))
        errors.extend(_check_sdist_pkg_info_matches_pyproject(sdists[0]))
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


def _is_valid_extra_name(name: str) -> bool:
    return EXTRA_NAME_RE.fullmatch(name) is not None


def _invalid_extra_names(values: Sequence[str]) -> list[str]:
    return [value for value in values if not _is_valid_extra_name(value)]


def _is_valid_distribution_name(name: str) -> bool:
    try:
        canonicalize_name(name, validate=True)
    except InvalidName:
        return False
    return True


def _is_valid_version(version: str) -> bool:
    if version.strip() != version:
        return False
    try:
        Version(version)
    except InvalidVersion:
        return False
    return True


def _is_valid_python_specifier(requires_python: str) -> bool:
    if requires_python.strip() != requires_python:
        return False
    try:
        SpecifierSet(requires_python)
    except InvalidSpecifier:
        return False
    return True


def _is_valid_license_expression(license_expression: str) -> bool:
    if license_expression.strip() != license_expression:
        return False
    try:
        canonicalize_license_expression(license_expression)
    except InvalidLicenseExpression:
        return False
    return True


def _is_valid_pyproject_description(description: str) -> bool:
    return bool(description.strip()) and description.strip() == description


def _format_pyproject_dependency_value(dependency: object) -> str:
    return dependency if isinstance(dependency, str) else repr(dependency)


def _is_valid_pyproject_dependency(dependency: str) -> bool:
    return dependency.strip() == dependency and _requirement_signature(dependency) is not None


def _artifact_names(artifact: Path) -> list[str]:
    if artifact.suffix == ".whl":
        with zipfile.ZipFile(artifact) as wheel:
            return wheel.namelist()
    if artifact.name.endswith((".tar.gz", ".tgz")):
        with tarfile.open(artifact) as source:
            return source.getnames()
    raise ValueError(f"unsupported distribution artifact: {artifact}")


def _artifact_text(artifact: Path, name: str) -> str:
    try:
        return _artifact_bytes(artifact, name).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{artifact}: invalid UTF-8 text in {name}") from exc


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


def _is_generated_cache_file(name: str) -> bool:
    return "/__pycache__/" in f"/{name}" or name.endswith(".pyc") or name.endswith(".pyo")


def _is_generated_metadata_file(name: str) -> bool:
    filename = name.rsplit("/", 1)[-1]
    return (
        filename == ".DS_Store"
        or filename.startswith("._")
        or name == "__MACOSX"
        or name.startswith("__MACOSX/")
    )


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
    errors.extend(_check_wheel_required_license_regular_files(artifact))
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
        errors.extend(_check_wheel_metadata_license_files(artifact, metadata_name))
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
        if not any(
            _is_wheel_dist_info_member_with_suffix(name, suffix) for name in normalized_names
        )
    ]


def _check_wheel_required_license_regular_files(artifact: Path) -> list[str]:
    errors: list[str] = []
    with zipfile.ZipFile(artifact) as wheel:
        members = wheel.infolist()
    for suffix in REQUIRED_WHEEL_LICENSE_SUFFIXES:
        matches = [
            member
            for member in members
            if _is_wheel_dist_info_member_with_suffix(member.filename, suffix)
        ]
        if matches and not any(_wheel_member_type(member) == "file" for member in matches):
            errors.append(
                f"{artifact}: required wheel license file is not a regular file "
                f"{suffix.rsplit('/', 1)[1]}"
            )
    return errors


def _wheel_has_regular_suffix(artifact: Path, suffix: str) -> bool:
    with zipfile.ZipFile(artifact) as wheel:
        return any(
            _is_wheel_dist_info_file_with_suffix(member.filename, suffix)
            and _wheel_member_type(member) == "file"
            for member in wheel.infolist()
        )


def _wheel_has_regular_member_suffix(artifact: Path, suffix: str) -> bool:
    with zipfile.ZipFile(artifact) as wheel:
        return any(
            _is_wheel_dist_info_member_with_suffix(member.filename, suffix)
            and _wheel_member_type(member) == "file"
            for member in wheel.infolist()
        )


def _check_wheel_metadata_license_files(artifact: Path, metadata_name: str) -> list[str]:
    metadata = Parser().parsestr(_artifact_text(artifact, metadata_name))
    metadata_license_files = set(metadata.get_all("License-File") or [])
    return [
        f"{artifact}: wheel missing metadata license file {license_file}"
        for license_file in sorted(metadata_license_files)
        if not _is_unsafe_archive_name(license_file, strip_root=False)
        and not _wheel_has_regular_member_suffix(artifact, f".dist-info/licenses/{license_file}")
    ]


def _wheel_member_type(member: zipfile.ZipInfo) -> str:
    name = member.filename.strip("/")
    if member.filename.endswith("/"):
        return "directory"
    unix_mode = member.external_attr >> 16
    file_type = stat.S_IFMT(unix_mode)
    if file_type in {0, stat.S_IFREG}:
        return "file"
    return "other"


def _check_wheel_allowed_top_level_paths(artifact: Path, archive_names: list[str]) -> list[str]:
    dist_info_directories = _wheel_dist_info_directories(archive_names)
    allowed_roots = {REQUIRED_TOP_LEVEL_PACKAGE, *dist_info_directories}
    errors: list[str] = []
    for name in sorted(archive_names):
        normalized_name = _normalize_archive_name(name, strip_root=False)
        if not normalized_name:
            continue
        root = normalized_name.split("/", 1)[0]
        if root not in allowed_roots:
            errors.append(f"{artifact}: wheel contains unexpected top-level path {normalized_name}")
    return errors


def _check_sdist_allowed_top_level_paths(artifact: Path, normalized_names: set[str]) -> list[str]:
    allowed_roots = {REQUIRED_TOP_LEVEL_PACKAGE, *REQUIRED_SDIST_REGULAR_FILES, "setup.cfg"}
    allowed_license_files = _sdist_declared_license_files(artifact)
    artifact_identity = _artifact_identity(artifact)
    egg_info_root = None
    if artifact_identity is not None:
        egg_info_root = f"{artifact_identity[0].replace('-', '_')}.egg-info"
        allowed_roots.add(egg_info_root)
    errors = [
        f"{artifact}: sdist contains unexpected top-level path {name}"
        for name in sorted(normalized_names)
        if name not in allowed_license_files and name.split("/", 1)[0] not in allowed_roots
    ]
    if egg_info_root is not None:
        errors.extend(
            _check_sdist_allowed_egg_info_paths(artifact, normalized_names, egg_info_root)
        )
        errors.extend(_check_sdist_metadata_regular_files(artifact, egg_info_root))
        errors.extend(_check_sdist_dependency_links(artifact, egg_info_root))
        errors.extend(_check_sdist_requires_txt(artifact, egg_info_root))
        errors.extend(_check_sdist_egg_info_pkg_info(artifact, egg_info_root))
        errors.extend(_check_sdist_sources_list(artifact, normalized_names, egg_info_root))
        errors.extend(_check_sdist_top_level(artifact, egg_info_root))
        errors.extend(_check_sdist_entry_points(artifact, egg_info_root))
    return errors


def _sdist_declared_license_files(artifact: Path) -> set[str]:
    try:
        project = _sdist_pyproject_project(artifact)
    except (OSError, tarfile.TarError, ValueError, zipfile.BadZipFile):
        return set()
    if project is None:
        return set()
    license_files = project.get("license-files")
    if not isinstance(license_files, list):
        return set()
    return {
        path
        for path in license_files
        if isinstance(path, str) and not _is_unsafe_archive_name(path, strip_root=False)
    }


def _check_sdist_allowed_egg_info_paths(
    artifact: Path,
    normalized_names: set[str],
    egg_info_root: str,
) -> list[str]:
    allowed_paths = {f"{egg_info_root}/{name}" for name in ALLOWED_SDIST_EGG_INFO_FILES}
    return [
        f"{artifact}: sdist contains unexpected egg-info file {name}"
        for name in sorted(normalized_names)
        if name.startswith(f"{egg_info_root}/") and name not in allowed_paths
    ]


def _check_sdist_metadata_regular_files(artifact: Path, egg_info_root: str) -> list[str]:
    metadata_paths = {f"{egg_info_root}/{name}" for name in ALLOWED_SDIST_EGG_INFO_FILES}
    metadata_paths.add("setup.cfg")
    member_types: dict[str, set[str]] = {}
    with tarfile.open(artifact) as source:
        for member in source.getmembers():
            name = _normalize_archive_name(member.name, strip_root=True)
            if name in metadata_paths:
                member_types.setdefault(name, set()).add(_sdist_member_type(member))
    return [
        f"{artifact}: sdist metadata file is not a regular file {name}"
        for name, types in sorted(member_types.items())
        if "file" not in types
    ]


def _check_sdist_dependency_links(artifact: Path, egg_info_root: str) -> list[str]:
    dependency_links_name = f"{egg_info_root}/dependency_links.txt"
    if not _sdist_has_regular_file(artifact, dependency_links_name):
        return []
    if _sdist_text(artifact, dependency_links_name).strip():
        return [f"{artifact}: sdist dependency_links.txt must be empty"]
    return []


def _check_sdist_requires_txt(artifact: Path, egg_info_root: str) -> list[str]:
    requires_name = f"{egg_info_root}/requires.txt"
    if not _sdist_has_regular_file(artifact, requires_name):
        return []
    try:
        project = _sdist_pyproject_project(artifact)
    except (OSError, tarfile.TarError, ValueError, tomllib.TOMLDecodeError):
        return []
    if project is None:
        return []

    (
        required_dependencies,
        optional_dependencies,
        invalid_dependencies,
        invalid_sections,
        direct_url_dependencies,
        direct_url_optional_dependencies,
    ) = _parse_sdist_requires_txt(_sdist_text(artifact, requires_name))
    errors = [
        f"{artifact}: invalid sdist requires.txt dependency {dependency}"
        for dependency in invalid_dependencies
    ]
    errors.extend(
        f"{artifact}: invalid sdist requires.txt section {section}" for section in invalid_sections
    )
    errors.extend(
        f"{artifact}: sdist requires.txt direct URL dependency {name}"
        for name in sorted(direct_url_dependencies)
    )
    errors.extend(
        f"{artifact}: sdist requires.txt direct URL optional dependency {extra}:{name}"
        for extra, name in sorted(direct_url_optional_dependencies)
    )
    errors.extend(
        _check_sdist_required_dependencies_match_pyproject(
            artifact,
            project,
            required_dependencies,
        )
    )
    errors.extend(
        _check_sdist_optional_dependencies_match_pyproject(
            artifact,
            project,
            optional_dependencies,
        )
    )
    return errors


def _check_sdist_required_dependencies_match_pyproject(
    artifact: Path,
    project: dict,
    required_dependencies: list[tuple[str, RequirementSignature]],
) -> list[str]:
    dependencies = project.get("dependencies")
    if not isinstance(dependencies, list):
        return []
    expected_dependencies = {
        signature[0]: signature
        for dependency in dependencies
        if isinstance(dependency, str)
        for signature in [_requirement_signature(dependency)]
        if signature is not None
    }
    dependency_names = [name for name, _signature in required_dependencies]
    actual_dependencies = {name: signature for name, signature in required_dependencies}
    errors = [
        f"{artifact}: sdist requires.txt duplicate required dependency {name}"
        for name, count in sorted(Counter(dependency_names).items())
        if count > 1
    ]
    errors.extend(
        f"{artifact}: sdist requires.txt missing required dependency {name}"
        for name in sorted(expected_dependencies.keys() - actual_dependencies.keys())
    )
    errors.extend(
        f"{artifact}: sdist requires.txt unexpected required dependency {name}"
        for name in sorted(actual_dependencies.keys() - expected_dependencies.keys())
    )
    errors.extend(
        f"{artifact}: sdist requires.txt dependency mismatch for {name}: "
        f"pyproject {_format_requirement_signature(expected)}, "
        f"requires.txt {_format_requirement_signature(actual_dependencies[name])}"
        for name, expected in sorted(expected_dependencies.items())
        if name in actual_dependencies and actual_dependencies[name] != expected
    )
    return errors


def _check_sdist_optional_dependencies_match_pyproject(
    artifact: Path,
    project: dict,
    optional_dependencies: list[tuple[tuple[str, str], RequirementSignature]],
) -> list[str]:
    optional_project_dependencies = project.get("optional-dependencies")
    if not isinstance(optional_project_dependencies, dict):
        return []
    expected_optional_dependencies = {
        (normalized_extra, signature[0]): signature
        for extra, dependencies_for_extra in optional_project_dependencies.items()
        if isinstance(extra, str) and isinstance(dependencies_for_extra, list)
        for normalized_extra in [_normalize_distribution_name(extra)]
        for dependency in dependencies_for_extra
        if isinstance(dependency, str)
        for signature in [_requirement_signature(dependency)]
        if signature is not None
    }
    optional_dependency_keys = [key for key, _signature in optional_dependencies]
    actual_optional_dependencies = {key: signature for key, signature in optional_dependencies}
    errors = [
        f"{artifact}: sdist requires.txt duplicate optional dependency {extra}:{name}"
        for (extra, name), count in sorted(Counter(optional_dependency_keys).items())
        if count > 1
    ]
    errors.extend(
        f"{artifact}: sdist requires.txt missing optional dependency {extra}:{name}"
        for extra, name in sorted(
            expected_optional_dependencies.keys() - actual_optional_dependencies.keys()
        )
    )
    errors.extend(
        f"{artifact}: sdist requires.txt unexpected optional dependency {extra}:{name}"
        for extra, name in sorted(
            actual_optional_dependencies.keys() - expected_optional_dependencies.keys()
        )
    )
    errors.extend(
        f"{artifact}: sdist requires.txt optional dependency mismatch for {extra}:{name}: "
        f"pyproject {_format_requirement_signature(expected)}, "
        f"requires.txt {_format_requirement_signature(actual_optional_dependencies[key])}"
        for key, expected in sorted(expected_optional_dependencies.items())
        for extra, name in [key]
        if key in actual_optional_dependencies and actual_optional_dependencies[key] != expected
    )
    return errors


def _parse_sdist_requires_txt(
    text: str,
) -> tuple[
    list[tuple[str, RequirementSignature]],
    list[tuple[tuple[str, str], RequirementSignature]],
    list[str],
    list[str],
    list[str],
    list[tuple[str, str]],
]:
    required_dependencies: list[tuple[str, RequirementSignature]] = []
    optional_dependencies: list[tuple[tuple[str, str], RequirementSignature]] = []
    invalid_dependencies: list[str] = []
    invalid_sections: list[str] = []
    direct_url_dependencies: list[str] = []
    direct_url_optional_dependencies: list[tuple[str, str]] = []
    current_extra = None
    current_marker = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            extra, separator, marker = section.partition(":")
            if not _valid_requires_txt_section(extra.strip(), marker.strip() if separator else ""):
                invalid_sections.append(line)
                current_extra = None
                current_marker = ""
                continue
            current_extra = _normalize_distribution_name(extra.strip())
            current_marker = marker.strip() if separator else ""
            continue
        requirement = _requirement_with_section_marker(line, current_marker)
        signature = _requirement_signature(requirement)
        if signature is None:
            invalid_dependencies.append(line)
            continue
        direct_url_name = _requirement_direct_url_name(requirement)
        if current_extra is None:
            required_dependencies.append((signature[0], signature))
            if direct_url_name is not None:
                direct_url_dependencies.append(direct_url_name)
        else:
            optional_dependencies.append(((current_extra, signature[0]), signature))
            if direct_url_name is not None:
                direct_url_optional_dependencies.append((current_extra, direct_url_name))
    return (
        required_dependencies,
        optional_dependencies,
        invalid_dependencies,
        invalid_sections,
        direct_url_dependencies,
        direct_url_optional_dependencies,
    )


def _valid_requires_txt_section(extra: str, marker: str) -> bool:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", extra):
        return False
    if not marker:
        return True
    return _requirement_signature(f"pytest; {marker}") is not None


def _requirement_with_section_marker(requirement: str, section_marker: str) -> str:
    if not section_marker:
        return requirement
    requirement_text, separator, requirement_marker = requirement.partition(";")
    marker = (
        section_marker if not separator else f"{requirement_marker.strip()} and {section_marker}"
    )
    return f"{requirement_text.strip()}; {marker}"


def _check_sdist_egg_info_pkg_info(artifact: Path, egg_info_root: str) -> list[str]:
    egg_info_pkg_info = f"{egg_info_root}/PKG-INFO"
    if not _sdist_has_regular_file(artifact, "PKG-INFO") or not _sdist_has_regular_file(
        artifact,
        egg_info_pkg_info,
    ):
        return []
    if _sdist_text(artifact, egg_info_pkg_info) != _sdist_text(artifact, "PKG-INFO"):
        return [f"{artifact}: sdist egg-info PKG-INFO does not match root PKG-INFO"]
    return []


def _check_sdist_sources_list(
    artifact: Path,
    normalized_names: set[str],
    egg_info_root: str,
) -> list[str]:
    sources_name = f"{egg_info_root}/SOURCES.txt"
    if not _sdist_has_regular_file(artifact, sources_name):
        return []
    errors: list[str] = []
    seen_sources: set[str] = set()
    for raw_line in _sdist_text(artifact, sources_name).splitlines():
        source_name = raw_line.strip()
        if not source_name:
            continue
        if source_name in seen_sources:
            errors.append(f"{artifact}: sdist SOURCES.txt duplicate entry {source_name}")
            continue
        seen_sources.add(source_name)
        if _is_unsafe_archive_name(source_name, strip_root=False):
            errors.append(f"{artifact}: unsafe sdist SOURCES.txt entry {source_name}")
            continue
        if source_name not in normalized_names:
            errors.append(
                f"{artifact}: sdist SOURCES.txt references missing archive " f"entry {source_name}"
            )
    archive_source_names = _sdist_regular_file_names(artifact) - {"PKG-INFO", "setup.cfg"}
    errors.extend(
        f"{artifact}: sdist SOURCES.txt missing archive entry {name}"
        for name in sorted(archive_source_names - seen_sources)
    )
    return errors


def _sdist_regular_file_names(artifact: Path) -> set[str]:
    names: set[str] = set()
    with tarfile.open(artifact) as source:
        for member in source.getmembers():
            name = _normalize_archive_name(member.name, strip_root=True)
            if name and member.isfile():
                names.add(name)
    return names


def _check_sdist_top_level(artifact: Path, egg_info_root: str) -> list[str]:
    top_level_name = f"{egg_info_root}/top_level.txt"
    if not _sdist_has_regular_file(artifact, top_level_name):
        return []
    top_level_package_items = [
        line.strip() for line in _sdist_text(artifact, top_level_name).splitlines() if line.strip()
    ]
    top_level_packages = set(top_level_package_items)
    errors: list[str] = []
    errors.extend(
        f"{artifact}: sdist top_level.txt duplicate top-level package {package}"
        for package, count in sorted(Counter(top_level_package_items).items())
        if count > 1
    )
    if REQUIRED_TOP_LEVEL_PACKAGE not in top_level_packages:
        errors.append(
            f"{artifact}: sdist top_level.txt missing required top-level "
            f"package {REQUIRED_TOP_LEVEL_PACKAGE}"
        )
    errors.extend(
        f"{artifact}: sdist top_level.txt unexpected top-level package {package}"
        for package in sorted(top_level_packages - {REQUIRED_TOP_LEVEL_PACKAGE})
    )
    return errors


def _check_sdist_entry_points(artifact: Path, egg_info_root: str) -> list[str]:
    entry_points_name = f"{egg_info_root}/entry_points.txt"
    if not _sdist_has_regular_file(artifact, entry_points_name):
        return []
    parser = _entry_points_parser()
    try:
        parser.read_string(_sdist_text(artifact, entry_points_name))
    except configparser.Error as exc:
        return [f"{artifact}: invalid sdist entry_points.txt: {exc}"]
    script_name, script_target = REQUIRED_CONSOLE_SCRIPT
    errors: list[str] = []
    if not parser.has_section("console_scripts"):
        return [
            f"{artifact}: sdist entry_points.txt missing console script "
            f"{script_name} = {script_target}"
        ]
    if parser.get("console_scripts", script_name, fallback=None) != script_target:
        errors.append(
            f"{artifact}: sdist entry_points.txt missing console script "
            f"{script_name} = {script_target}"
        )
    errors.extend(
        f"{artifact}: sdist entry_points.txt unexpected entry point section {section}"
        for section in sorted(set(parser.sections()) - {"console_scripts"})
    )
    errors.extend(
        f"{artifact}: sdist entry_points.txt unexpected console script {name} = {target}"
        for name, target in sorted(parser.items("console_scripts"))
        if name != script_name
    )
    return errors


def _sdist_text(artifact: Path, normalized_name: str) -> str:
    with tarfile.open(artifact) as source:
        for member in source.getmembers():
            name = _normalize_archive_name(member.name, strip_root=True)
            if name != normalized_name:
                continue
            file = source.extractfile(member)
            if file is None:
                raise ValueError(f"missing archive member: {normalized_name}")
            with file:
                try:
                    return file.read().decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ValueError(
                        f"{artifact}: invalid UTF-8 text in {normalized_name}"
                    ) from exc
    raise ValueError(f"missing archive member: {normalized_name}")


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


def _check_wheel_metadata_matches_sdist_pyproject(wheel: Path, sdist: Path) -> list[str]:
    try:
        wheel_names = _artifact_names(wheel)
        sdist_names = _artifact_names(sdist)
    except (OSError, tarfile.TarError, ValueError, zipfile.BadZipFile, tomllib.TOMLDecodeError):
        return []
    metadata_name = _single_archive_name_with_suffix(wheel_names, ".dist-info/METADATA")
    pyproject_name = _single_normalized_archive_name(sdist_names, "pyproject.toml")
    if metadata_name is None or pyproject_name is None:
        return []
    try:
        pyproject = tomllib.loads(_artifact_text(sdist, pyproject_name))
    except (OSError, tarfile.TarError, ValueError, tomllib.TOMLDecodeError):
        return []
    project = pyproject.get("project")
    if not isinstance(project, dict):
        return []

    metadata = Parser().parsestr(_artifact_text(wheel, metadata_name))
    return _check_core_metadata_matches_pyproject(
        wheel,
        metadata,
        project,
        artifact_label="wheel",
        metadata_label="METADATA",
    )


def _check_sdist_pkg_info_matches_pyproject(sdist: Path) -> list[str]:
    try:
        project = _sdist_pyproject_project(sdist)
        sdist_names = _artifact_names(sdist)
    except (OSError, tarfile.TarError, ValueError, zipfile.BadZipFile):
        return []
    pkg_info_name = _single_normalized_archive_name(sdist_names, "PKG-INFO")
    if pkg_info_name is None or project is None:
        return []
    if not _sdist_has_regular_file(sdist, "PKG-INFO"):
        return []
    try:
        metadata = Parser().parsestr(_artifact_text(sdist, pkg_info_name))
    except (OSError, tarfile.TarError, ValueError):
        return []

    return [
        *_check_pyproject_field_validity(sdist, project),
        *_check_pyproject_dependency_duplicates(sdist, project),
        *_check_pyproject_required_core_dependencies(sdist, project),
        *_check_sdist_pyproject_license_files(sdist, project),
        *_check_sdist_pyproject_readme_file(sdist, project),
        *_check_core_metadata_matches_pyproject(
            sdist,
            metadata,
            project,
            artifact_label="sdist",
            metadata_label="PKG-INFO",
        ),
    ]


def _sdist_pyproject_project(sdist: Path) -> dict | None:
    sdist_names = _artifact_names(sdist)
    pyproject_name = _single_normalized_archive_name(sdist_names, "pyproject.toml")
    if pyproject_name is None or not _sdist_has_regular_file(sdist, "pyproject.toml"):
        return None
    pyproject = tomllib.loads(_artifact_text(sdist, pyproject_name))
    project = pyproject.get("project")
    return project if isinstance(project, dict) else None


def _check_sdist_pyproject_license_files(sdist: Path, project: dict) -> list[str]:
    license_files = project.get("license-files")
    if not isinstance(license_files, list):
        return []
    return [
        f"{sdist}: sdist missing pyproject license file {license_file}"
        for license_file in sorted({path for path in license_files if isinstance(path, str)})
        if not _is_unsafe_archive_name(license_file, strip_root=False)
        and not _sdist_has_regular_file(sdist, license_file)
    ]


def _check_sdist_pyproject_readme_file(sdist: Path, project: dict) -> list[str]:
    readme = project.get("readme")
    if (
        not isinstance(readme, str)
        or _pyproject_readme_content_type(readme) is None
        or not _is_valid_pyproject_file_path(readme)
    ):
        return []
    if _sdist_has_regular_file(sdist, readme):
        return []
    return [f"{sdist}: sdist missing pyproject readme file {readme}"]


def _check_pyproject_unsupported_fields(sdist: Path, project: dict) -> list[str]:
    return [
        f"{sdist}: pyproject unsupported metadata field {field}"
        for field in UNSUPPORTED_PYPROJECT_FIELDS
        if field in project
    ]


def _check_pyproject_required_dependency_fields(sdist: Path, project: dict) -> list[str]:
    dependencies = project.get("dependencies")
    if not isinstance(dependencies, list):
        return []
    errors = [
        f"{sdist}: pyproject invalid required dependency "
        f"{_format_pyproject_dependency_value(dependency)}"
        for dependency in dependencies
        if not isinstance(dependency, str) or not _is_valid_pyproject_dependency(dependency)
    ]
    errors.extend(
        f"{sdist}: pyproject direct URL dependency {name}"
        for dependency in dependencies
        if isinstance(dependency, str)
        for name in [_requirement_direct_url_name(dependency)]
        if name is not None
    )
    return errors


def _check_pyproject_description_field(sdist: Path, project: dict) -> list[str]:
    description = project.get("description")
    if description is not None and not isinstance(description, str):
        return [f"{sdist}: pyproject invalid description {description!r}"]
    if isinstance(description, str) and not _is_valid_pyproject_description(description):
        return [f"{sdist}: pyproject invalid description {description!r}"]
    return []


def _check_pyproject_license_field(sdist: Path, project: dict) -> list[str]:
    license_expression = project.get("license")
    if license_expression is not None and not isinstance(license_expression, str):
        return [f"{sdist}: pyproject invalid license {license_expression!r}"]
    if isinstance(license_expression, str) and not _is_valid_license_expression(license_expression):
        return [f"{sdist}: pyproject invalid license expression {license_expression!r}"]
    return []


def _check_pyproject_keyword_field(sdist: Path, project: dict) -> list[str]:
    keywords = project.get("keywords")
    if isinstance(keywords, list):
        errors = [
            f"{sdist}: pyproject invalid keyword {keyword!r}"
            for keyword in keywords
            if not isinstance(keyword, str) or not _is_valid_pyproject_keyword(keyword)
        ]
        errors.extend(
            f"{sdist}: pyproject duplicate keyword {keyword}"
            for keyword, count in sorted(
                Counter(keyword for keyword in keywords if isinstance(keyword, str)).items()
            )
            if count > 1
        )
        return errors
    if keywords is not None:
        return [f"{sdist}: pyproject invalid keywords list {keywords!r}"]
    return []


def _check_pyproject_classifier_field(sdist: Path, project: dict) -> list[str]:
    classifiers = project.get("classifiers")
    if isinstance(classifiers, list):
        errors = [
            f"{sdist}: pyproject invalid classifier {classifier!r}"
            for classifier in classifiers
            if not isinstance(classifier, str) or not _is_valid_pyproject_classifier(classifier)
        ]
        errors.extend(
            f"{sdist}: pyproject duplicate classifier {classifier}"
            for classifier, count in sorted(
                Counter(
                    classifier for classifier in classifiers if isinstance(classifier, str)
                ).items()
            )
            if count > 1
        )
        return errors
    if classifiers is not None:
        return [f"{sdist}: pyproject invalid classifiers list {classifiers!r}"]
    return []


def _check_pyproject_dynamic_field(sdist: Path, project: dict) -> list[str]:
    dynamic_fields = project.get("dynamic")
    if isinstance(dynamic_fields, list):
        errors = [
            f"{sdist}: pyproject invalid dynamic metadata field {field!r}"
            for field in dynamic_fields
            if not isinstance(field, str) or not _is_valid_pyproject_dynamic_field(field)
        ]
        normalized_dynamic_fields = [
            field.strip().lower()
            for field in dynamic_fields
            if isinstance(field, str) and _is_valid_pyproject_dynamic_field(field)
        ]
        errors.extend(
            f"{sdist}: pyproject duplicate dynamic metadata field {field}"
            for field, count in sorted(Counter(normalized_dynamic_fields).items())
            if count > 1
        )
        errors.extend(
            f"{sdist}: pyproject unexpected dynamic metadata field {field}"
            for field in sorted(set(normalized_dynamic_fields))
        )
        return errors
    if dynamic_fields is not None:
        return [f"{sdist}: pyproject invalid dynamic metadata list {dynamic_fields!r}"]
    return []


def _check_pyproject_readme_field(sdist: Path, project: dict) -> list[str]:
    readme = project.get("readme")
    if readme is not None and not isinstance(readme, str):
        return [f"{sdist}: pyproject invalid readme {readme!r}"]
    if isinstance(readme, str) and not _is_valid_pyproject_file_path(readme):
        return [f"{sdist}: pyproject invalid readme path {readme!r}"]
    if isinstance(readme, str) and _pyproject_readme_content_type(readme) is None:
        return [f"{sdist}: pyproject invalid readme content type {readme}"]
    return []


def _check_pyproject_license_file_field(sdist: Path, project: dict) -> list[str]:
    license_files = project.get("license-files")
    if isinstance(license_files, list):
        errors = [
            f"{sdist}: pyproject invalid license file {license_file!r}"
            for license_file in license_files
            if not isinstance(license_file, str)
        ]
        errors.extend(
            f"{sdist}: pyproject duplicate license file {license_file}"
            for license_file, count in sorted(
                Counter(path for path in license_files if isinstance(path, str)).items()
            )
            if count > 1
        )
        errors.extend(
            f"{sdist}: pyproject invalid license file path {license_file!r}"
            for license_file in license_files
            if isinstance(license_file, str) and not _is_valid_pyproject_file_path(license_file)
        )
        return errors
    if license_files is not None:
        return [f"{sdist}: pyproject invalid license files list {license_files!r}"]
    return []


def _check_pyproject_author_field(sdist: Path, project: dict) -> list[str]:
    authors = project.get("authors")
    if isinstance(authors, list):
        errors = [
            f"{sdist}: pyproject invalid author {author!r}"
            for author in authors
            if not isinstance(author, dict)
            or not any(field in author for field in SUPPORTED_PYPROJECT_AUTHOR_FIELDS)
        ]
        errors.extend(
            f"{sdist}: pyproject unsupported author field {field}"
            for author in authors
            if isinstance(author, dict)
            for field in sorted(author)
            if field not in SUPPORTED_PYPROJECT_AUTHOR_FIELDS
        )
        errors.extend(
            f"{sdist}: pyproject invalid author name {author['name']!r}"
            for author in authors
            if isinstance(author, dict)
            and "name" in author
            and (
                not isinstance(author["name"], str)
                or not _is_valid_pyproject_author_name(author["name"])
            )
        )
        errors.extend(
            f"{sdist}: pyproject invalid author email {author['email']!r}"
            for author in authors
            if isinstance(author, dict)
            and "email" in author
            and (
                not isinstance(author["email"], str) or not _is_valid_author_email(author["email"])
            )
        )
        author_identities = [
            identity
            for author in authors
            if isinstance(author, dict)
            for identity in [_pyproject_author_identity(author)]
            if identity is not None
        ]
        author_identity_labels = {
            _normalize_author_identity(identity): identity for identity in author_identities
        }
        errors.extend(
            f"{sdist}: pyproject duplicate author {author_identity_labels[identity]}"
            for identity, count in sorted(
                Counter(
                    _normalize_author_identity(identity) for identity in author_identities
                ).items()
            )
            if count > 1
        )
        return errors
    if authors is not None:
        return [f"{sdist}: pyproject invalid authors list {authors!r}"]
    return []


def _check_pyproject_url_field(sdist: Path, project: dict) -> list[str]:
    project_urls = project.get("urls")
    if isinstance(project_urls, dict):
        errors = [
            f"{sdist}: pyproject invalid project URL label {label!r}"
            for label in sorted(project_urls)
            if not _is_valid_project_url_label(label)
        ]
        errors.extend(
            f"{sdist}: pyproject duplicate project URL label {label}"
            for label, count in sorted(
                Counter(_normalize_project_url_label(label) for label in project_urls).items()
            )
            if count > 1
        )
        errors.extend(
            f"{sdist}: pyproject invalid project URL {label}:{url!r}"
            for label, url in sorted(project_urls.items())
            if not isinstance(url, str) or not _is_valid_project_url(url)
        )
        return errors
    if project_urls is not None:
        return [f"{sdist}: pyproject invalid project URLs table {project_urls!r}"]
    return []


def _check_pyproject_optional_dependency_fields(sdist: Path, project: dict) -> list[str]:
    optional_dependencies = project.get("optional-dependencies")
    if optional_dependencies is not None and not isinstance(optional_dependencies, dict):
        return [f"{sdist}: pyproject invalid optional dependencies table"]
    if not isinstance(optional_dependencies, dict):
        return []

    errors = [
        f"{sdist}: pyproject invalid optional dependency extra {extra}"
        for extra in optional_dependencies
        if isinstance(extra, str) and not _is_valid_extra_name(extra)
    ]
    normalized_extras = [
        _normalize_distribution_name(extra)
        for extra in optional_dependencies
        if isinstance(extra, str)
    ]
    errors.extend(
        f"{sdist}: pyproject duplicate optional dependency extra {extra}"
        for extra, count in sorted(Counter(normalized_extras).items())
        if count > 1
    )
    errors.extend(
        f"{sdist}: pyproject invalid optional dependency list "
        f"{_normalize_distribution_name(extra)}"
        for extra, dependencies_for_extra in optional_dependencies.items()
        if isinstance(extra, str) and not isinstance(dependencies_for_extra, list)
    )
    errors.extend(
        f"{sdist}: pyproject invalid optional dependency "
        f"{_normalize_distribution_name(extra)}:"
        f"{_format_pyproject_dependency_value(dependency)}"
        for extra, dependencies_for_extra in optional_dependencies.items()
        if isinstance(extra, str) and isinstance(dependencies_for_extra, list)
        for dependency in dependencies_for_extra
        if not isinstance(dependency, str) or not _is_valid_pyproject_dependency(dependency)
    )
    errors.extend(
        f"{sdist}: pyproject direct URL optional dependency "
        f"{_normalize_distribution_name(extra)}:{name}"
        for extra, dependencies_for_extra in optional_dependencies.items()
        if isinstance(extra, str) and isinstance(dependencies_for_extra, list)
        for dependency in dependencies_for_extra
        if isinstance(dependency, str)
        for name in [_requirement_direct_url_name(dependency)]
        if name is not None
    )
    return errors


def _check_pyproject_field_validity(sdist: Path, project: dict) -> list[str]:
    checks = (
        _check_pyproject_unsupported_fields,
        _check_pyproject_required_dependency_fields,
        _check_pyproject_description_field,
        _check_pyproject_license_field,
        _check_pyproject_keyword_field,
        _check_pyproject_classifier_field,
        _check_pyproject_dynamic_field,
        _check_pyproject_readme_field,
        _check_pyproject_license_file_field,
        _check_pyproject_author_field,
        _check_pyproject_url_field,
        _check_pyproject_optional_dependency_fields,
    )
    errors: list[str] = []
    for check in checks:
        errors.extend(check(sdist, project))
    return errors


def _check_pyproject_dependency_duplicates(sdist: Path, project: dict) -> list[str]:
    errors: list[str] = []
    dependencies = project.get("dependencies")
    if isinstance(dependencies, list):
        dependency_names = [
            signature[0]
            for dependency in dependencies
            if isinstance(dependency, str)
            for signature in [_requirement_signature(dependency)]
            if signature is not None
        ]
        errors.extend(
            f"{sdist}: pyproject duplicate required dependency {name}"
            for name, count in sorted(Counter(dependency_names).items())
            if count > 1
        )

    optional_dependencies = project.get("optional-dependencies")
    if isinstance(optional_dependencies, dict):
        optional_dependency_keys = [
            (normalized_extra, signature[0])
            for extra, dependencies_for_extra in optional_dependencies.items()
            if isinstance(extra, str) and isinstance(dependencies_for_extra, list)
            for normalized_extra in [_normalize_distribution_name(extra)]
            for dependency in dependencies_for_extra
            if isinstance(dependency, str)
            for signature in [_requirement_signature(dependency)]
            if signature is not None
        ]
        errors.extend(
            f"{sdist}: pyproject duplicate optional dependency {extra}:{name}"
            for (extra, name), count in sorted(Counter(optional_dependency_keys).items())
            if count > 1
        )
    return errors


def _check_pyproject_required_core_dependencies(sdist: Path, project: dict) -> list[str]:
    dependencies = project.get("dependencies")
    if not isinstance(dependencies, list):
        return []
    dependency_names = {
        signature[0]
        for dependency in dependencies
        if isinstance(dependency, str)
        for signature in [_requirement_signature(dependency)]
        if signature is not None
    }
    return [
        f"{sdist}: pyproject missing required core dependency {name}"
        for name in REQUIRED_CORE_DEPENDENCIES
        if name not in dependency_names
    ]


def _check_core_metadata_matches_pyproject(
    artifact: Path,
    metadata,
    project: dict,
    *,
    artifact_label: str,
    metadata_label: str,
) -> list[str]:
    metadata_checks = (
        _check_core_metadata_version,
        _check_singleton_core_metadata_fields,
        _check_license_file_metadata,
        _check_license_expression_metadata,
        _check_project_url_metadata,
        _check_classifier_metadata,
        _check_dynamic_metadata,
        _check_unsupported_core_metadata_fields,
    )
    errors: list[str] = []
    for check in metadata_checks:
        errors.extend(
            check(
                artifact,
                metadata,
                artifact_label=artifact_label,
                metadata_label=metadata_label,
            )
        )
    metadata_requires_dist = metadata.get_all("Requires-Dist") or []
    project_metadata_checks = (
        _check_project_scalar_core_metadata,
        _check_project_collection_core_metadata,
    )
    for project_check in project_metadata_checks:
        errors.extend(
            project_check(
                artifact,
                metadata,
                project,
                artifact_label=artifact_label,
                metadata_label=metadata_label,
            )
        )
    errors.extend(
        _check_project_dependency_metadata(
            artifact,
            metadata_requires_dist,
            project,
            artifact_label=artifact_label,
            metadata_label=metadata_label,
        )
    )
    errors.extend(
        _check_project_extra_metadata(
            artifact,
            metadata,
            metadata_requires_dist,
            project,
            artifact_label=artifact_label,
            metadata_label=metadata_label,
        )
    )
    return errors


def _check_project_scalar_core_metadata(
    artifact: Path,
    metadata,
    project: dict,
    *,
    artifact_label: str,
    metadata_label: str,
) -> list[str]:
    errors: list[str] = []
    requires_python = project.get("requires-python")
    if isinstance(requires_python, str):
        metadata_requires_python = metadata.get("Requires-Python")
        if metadata_requires_python != requires_python:
            errors.append(
                f"{artifact}: {artifact_label} {metadata_label} Requires-Python mismatch: "
                f"pyproject {requires_python}, "
                f"{metadata_label} {metadata_requires_python or '<missing>'}"
            )
    description = project.get("description")
    if isinstance(description, str):
        metadata_summary = metadata.get("Summary")
        if metadata_summary != description:
            errors.append(
                f"{artifact}: {artifact_label} {metadata_label} Summary mismatch: "
                f"pyproject {description}, {metadata_label} {metadata_summary or '<missing>'}"
            )
    expected_description_content_type = _pyproject_readme_content_type(project.get("readme"))
    if expected_description_content_type is not None:
        metadata_description_content_type = metadata.get("Description-Content-Type")
        if metadata_description_content_type != expected_description_content_type:
            errors.append(
                f"{artifact}: {artifact_label} {metadata_label} "
                f"Description-Content-Type mismatch: "
                f"pyproject {expected_description_content_type}, "
                f"{metadata_label} {metadata_description_content_type or '<missing>'}"
            )
    license_expression = project.get("license")
    if isinstance(license_expression, str):
        metadata_license_expression = metadata.get("License-Expression")
        if metadata_license_expression != license_expression:
            errors.append(
                f"{artifact}: {artifact_label} {metadata_label} License-Expression mismatch: "
                f"pyproject {license_expression}, "
                f"{metadata_label} {metadata_license_expression or '<missing>'}"
            )
    return errors


def _check_project_collection_core_metadata(
    artifact: Path,
    metadata,
    project: dict,
    *,
    artifact_label: str,
    metadata_label: str,
) -> list[str]:
    errors = _check_project_license_file_values(
        artifact,
        metadata,
        project,
        artifact_label=artifact_label,
        metadata_label=metadata_label,
    )
    errors.extend(
        _check_project_keyword_values(
            artifact,
            metadata,
            project,
            artifact_label=artifact_label,
            metadata_label=metadata_label,
        )
    )
    errors.extend(
        _check_project_classifier_values(
            artifact,
            metadata,
            project,
            artifact_label=artifact_label,
            metadata_label=metadata_label,
        )
    )
    errors.extend(
        _check_project_author_values(
            artifact,
            metadata,
            project,
            artifact_label=artifact_label,
            metadata_label=metadata_label,
        )
    )
    errors.extend(
        _check_project_url_values(
            artifact,
            metadata,
            project,
            artifact_label=artifact_label,
            metadata_label=metadata_label,
        )
    )
    return errors


def _check_project_license_file_values(
    artifact: Path,
    metadata,
    project: dict,
    *,
    artifact_label: str,
    metadata_label: str,
) -> list[str]:
    license_files = project.get("license-files")
    if not isinstance(license_files, list):
        return []
    expected_license_files = {path for path in license_files if isinstance(path, str)}
    metadata_license_file_values = metadata.get_all("License-File") or []
    metadata_license_files = set(metadata_license_file_values)
    errors = [
        f"{artifact}: {artifact_label} {metadata_label} " f"missing license file metadata {path}"
        for path in sorted(expected_license_files - metadata_license_files)
    ]
    errors.extend(
        f"{artifact}: {artifact_label} {metadata_label} " f"unexpected license file metadata {path}"
        for path in sorted(metadata_license_files - expected_license_files)
    )
    return errors


def _check_project_keyword_values(
    artifact: Path,
    metadata,
    project: dict,
    *,
    artifact_label: str,
    metadata_label: str,
) -> list[str]:
    keywords = project.get("keywords")
    if not isinstance(keywords, list):
        return []
    expected_keywords = [keyword for keyword in keywords if isinstance(keyword, str)]
    metadata_keywords = _metadata_keywords(metadata)
    if metadata_keywords == expected_keywords:
        return []
    return [
        f"{artifact}: {artifact_label} {metadata_label} Keywords mismatch: "
        f"pyproject {', '.join(expected_keywords) or '<empty>'}, "
        f"{metadata_label} {', '.join(metadata_keywords) or '<missing>'}"
    ]


def _check_project_classifier_values(
    artifact: Path,
    metadata,
    project: dict,
    *,
    artifact_label: str,
    metadata_label: str,
) -> list[str]:
    classifiers = project.get("classifiers")
    if not isinstance(classifiers, list):
        return []
    expected_classifiers = {classifier for classifier in classifiers if isinstance(classifier, str)}
    metadata_classifier_values = metadata.get_all("Classifier") or []
    metadata_classifiers = set(metadata_classifier_values)
    errors = [
        f"{artifact}: {artifact_label} {metadata_label} missing classifier {classifier}"
        for classifier in sorted(expected_classifiers - metadata_classifiers)
    ]
    errors.extend(
        f"{artifact}: {artifact_label} {metadata_label} unexpected classifier {classifier}"
        for classifier in sorted(metadata_classifiers - expected_classifiers)
    )
    return errors


def _check_project_author_values(
    artifact: Path,
    metadata,
    project: dict,
    *,
    artifact_label: str,
    metadata_label: str,
) -> list[str]:
    authors = project.get("authors")
    if not isinstance(authors, list):
        return []
    errors: list[str] = []
    expected_author = _pyproject_author_names(authors)
    if expected_author:
        metadata_author = metadata.get("Author")
        if metadata_author != expected_author:
            errors.append(
                f"{artifact}: {artifact_label} {metadata_label} Author mismatch: "
                f"pyproject {expected_author}, {metadata_label} {metadata_author or '<missing>'}"
            )
    expected_author_email = _pyproject_author_email(authors)
    if expected_author_email:
        metadata_author_email = metadata.get("Author-email")
        if metadata_author_email != expected_author_email:
            errors.append(
                f"{artifact}: {artifact_label} {metadata_label} "
                f"Author-email mismatch: "
                f"pyproject {expected_author_email}, "
                f"{metadata_label} {metadata_author_email or '<missing>'}"
            )
    return errors


def _check_project_url_values(
    artifact: Path,
    metadata,
    project: dict,
    *,
    artifact_label: str,
    metadata_label: str,
) -> list[str]:
    project_urls = project.get("urls")
    if not isinstance(project_urls, dict):
        return []
    expected_urls = {
        label: url
        for label, url in project_urls.items()
        if isinstance(label, str) and isinstance(url, str)
    }
    actual_urls = {label: url for label, url in _metadata_project_urls(metadata)}
    errors = [
        f"{artifact}: {artifact_label} {metadata_label} missing project URL {label}"
        for label in sorted(expected_urls.keys() - actual_urls.keys())
    ]
    errors.extend(
        f"{artifact}: {artifact_label} {metadata_label} unexpected project URL {label}"
        for label in sorted(actual_urls.keys() - expected_urls.keys())
    )
    errors.extend(
        f"{artifact}: {artifact_label} {metadata_label} project URL mismatch for {label}: "
        f"pyproject {expected}, {metadata_label} {actual_urls[label]}"
        for label, expected in sorted(expected_urls.items())
        if label in actual_urls and actual_urls[label] != expected
    )
    return errors


def _check_project_dependency_metadata(
    artifact: Path,
    metadata_requires_dist: Sequence[str],
    project: dict,
    *,
    artifact_label: str,
    metadata_label: str,
) -> list[str]:
    dependencies = project.get("dependencies")
    if not isinstance(dependencies, list):
        return []
    expected_dependencies = {
        signature[0]: signature
        for dependency in dependencies
        if isinstance(dependency, str)
        for signature in [_requirement_signature(dependency)]
        if signature is not None
    }
    errors = [
        f"{artifact}: {artifact_label} {metadata_label} "
        f"invalid Requires-Dist dependency {dependency}"
        for dependency in metadata_requires_dist
        if _requirement_signature(dependency) is None
    ]
    errors.extend(
        f"{artifact}: {artifact_label} {metadata_label} direct URL dependency {name}"
        for dependency in metadata_requires_dist
        if _requires_dist_extra(dependency) is None
        for name in [_requirement_direct_url_name(dependency)]
        if name is not None
    )
    metadata_dependency_items = [
        (signature[0], signature)
        for dependency in metadata_requires_dist
        if _requires_dist_extra(dependency) is None
        for signature in [_requirement_signature(dependency)]
        if signature is not None
    ]
    metadata_dependencies = {name: signature for name, signature in metadata_dependency_items}
    metadata_dependency_names = [name for name, _signature in metadata_dependency_items]
    errors.extend(
        f"{artifact}: {artifact_label} {metadata_label} duplicate required dependency {name}"
        for name, count in sorted(Counter(metadata_dependency_names).items())
        if count > 1
    )
    errors.extend(
        f"{artifact}: {artifact_label} {metadata_label} missing required dependency {name}"
        for name in sorted(expected_dependencies.keys() - metadata_dependencies.keys())
    )
    errors.extend(
        f"{artifact}: {artifact_label} {metadata_label} unexpected required dependency {name}"
        for name in sorted(metadata_dependencies.keys() - expected_dependencies.keys())
    )
    errors.extend(
        f"{artifact}: {artifact_label} {metadata_label} dependency mismatch for {name}: "
        f"pyproject {_format_requirement_signature(expected)}, "
        f"{metadata_label} {_format_requirement_signature(metadata_dependencies[name])}"
        for name, expected in sorted(expected_dependencies.items())
        if name in metadata_dependencies and metadata_dependencies[name] != expected
    )
    return errors


def _check_project_extra_metadata(
    artifact: Path,
    metadata,
    metadata_requires_dist: Sequence[str],
    project: dict,
    *,
    artifact_label: str,
    metadata_label: str,
) -> list[str]:
    optional_dependencies = project.get("optional-dependencies")
    expected_extras = (
        {
            _normalize_distribution_name(extra)
            for extra in optional_dependencies
            if isinstance(extra, str)
        }
        if isinstance(optional_dependencies, dict)
        else set()
    )
    metadata_provided_extra_values = metadata.get_all("Provides-Extra") or []
    errors = [
        f"{artifact}: {artifact_label} {metadata_label} invalid provided extra {extra}"
        for extra in _invalid_extra_names(metadata_provided_extra_values)
    ]
    wheel_extra_items = [
        _normalize_distribution_name(extra) for extra in metadata_provided_extra_values
    ]
    wheel_extras = set(wheel_extra_items)
    errors.extend(
        f"{artifact}: {artifact_label} {metadata_label} duplicate provided extra {extra}"
        for extra, count in sorted(Counter(wheel_extra_items).items())
        if count > 1
    )
    errors.extend(
        f"{artifact}: {artifact_label} {metadata_label} missing provided extra {extra}"
        for extra in sorted(expected_extras - wheel_extras)
    )
    errors.extend(
        f"{artifact}: {artifact_label} {metadata_label} unexpected provided extra {extra}"
        for extra in sorted(wheel_extras - expected_extras)
    )
    errors.extend(
        f"{artifact}: {artifact_label} {metadata_label} "
        f"invalid Requires-Dist extra marker {extra}"
        for extra in _invalid_requires_dist_extra_markers(metadata_requires_dist)
    )
    metadata_required_extras = {
        extra
        for dependency in metadata_requires_dist
        for extra in [_requires_dist_extra(dependency)]
        if extra is not None
    }
    errors.extend(
        f"{artifact}: {artifact_label} {metadata_label} "
        f"Requires-Dist references undeclared extra {extra}"
        for extra in sorted(metadata_required_extras - wheel_extras)
    )
    if isinstance(optional_dependencies, dict):
        errors.extend(
            _check_project_optional_dependency_metadata(
                artifact,
                metadata_requires_dist,
                optional_dependencies,
                artifact_label=artifact_label,
                metadata_label=metadata_label,
            )
        )
    return errors


def _check_project_optional_dependency_metadata(
    artifact: Path,
    metadata_requires_dist: Sequence[str],
    optional_dependencies: dict,
    *,
    artifact_label: str,
    metadata_label: str,
) -> list[str]:
    expected_optional_dependencies = {
        (normalized_extra, signature[0]): signature
        for extra, dependencies_for_extra in optional_dependencies.items()
        if isinstance(extra, str) and isinstance(dependencies_for_extra, list)
        for normalized_extra in [_normalize_distribution_name(extra)]
        for dependency in dependencies_for_extra
        if isinstance(dependency, str)
        for signature in [_requirement_signature(dependency)]
        if signature is not None
    }
    errors = [
        f"{artifact}: {artifact_label} {metadata_label} "
        f"direct URL optional dependency {extra}:{name}"
        for dependency in metadata_requires_dist
        for extra in [_requires_dist_extra(dependency)]
        if extra is not None
        for name in [_requirement_direct_url_name(dependency)]
        if name is not None
    ]
    metadata_optional_dependency_items = [
        ((extra, signature[0]), signature)
        for dependency in metadata_requires_dist
        for extra in [_requires_dist_extra(dependency)]
        if extra is not None
        for signature in [_requirement_signature(dependency)]
        if signature is not None
    ]
    metadata_optional_dependencies = {
        key: signature for key, signature in metadata_optional_dependency_items
    }
    metadata_optional_dependency_keys = [
        key for key, _signature in metadata_optional_dependency_items
    ]
    errors.extend(
        f"{artifact}: {artifact_label} {metadata_label} "
        f"duplicate optional dependency {extra}:{name}"
        for (extra, name), count in sorted(Counter(metadata_optional_dependency_keys).items())
        if count > 1
    )
    errors.extend(
        f"{artifact}: {artifact_label} {metadata_label} "
        f"missing optional dependency {extra}:{name}"
        for extra, name in sorted(
            expected_optional_dependencies.keys() - metadata_optional_dependencies.keys()
        )
    )
    errors.extend(
        f"{artifact}: {artifact_label} {metadata_label} "
        f"unexpected optional dependency {extra}:{name}"
        for extra, name in sorted(
            metadata_optional_dependencies.keys() - expected_optional_dependencies.keys()
        )
    )
    errors.extend(
        f"{artifact}: {artifact_label} {metadata_label} "
        f"optional dependency mismatch for {extra}:{name}: "
        f"pyproject {_format_requirement_signature(expected)}, "
        f"{metadata_label} {_format_requirement_signature(metadata_optional_dependencies[key])}"
        for key, expected in sorted(expected_optional_dependencies.items())
        for extra, name in [key]
        if key in metadata_optional_dependencies and metadata_optional_dependencies[key] != expected
    )
    return errors


def _check_classifier_metadata(
    artifact: Path,
    metadata,
    *,
    artifact_label: str,
    metadata_label: str,
) -> list[str]:
    metadata_classifier_values = metadata.get_all("Classifier") or []
    return [
        f"{artifact}: {artifact_label} {metadata_label} "
        f"duplicate classifier metadata {classifier}"
        for classifier, count in sorted(Counter(metadata_classifier_values).items())
        if count > 1
    ]


def _check_dynamic_metadata(
    artifact: Path,
    metadata,
    *,
    artifact_label: str,
    metadata_label: str,
) -> list[str]:
    return [
        f"{artifact}: {artifact_label} {metadata_label} "
        f"unexpected Dynamic metadata field {field}"
        for field in sorted(set(metadata.get_all("Dynamic") or []))
        if field.lower() not in ALLOWED_DYNAMIC_METADATA_FIELDS
    ]


def _check_unsupported_core_metadata_fields(
    artifact: Path,
    metadata,
    *,
    artifact_label: str,
    metadata_label: str,
) -> list[str]:
    return [
        f"{artifact}: {artifact_label} {metadata_label} "
        f"unsupported {field} metadata field {value}"
        for field in UNSUPPORTED_CORE_METADATA_FIELDS
        for value in metadata.get_all(field) or []
    ]


def _metadata_project_urls(metadata) -> list[tuple[str, str]]:
    project_urls: list[tuple[str, str]] = []
    for value in metadata.get_all("Project-URL") or []:
        label, separator, url = _split_project_url_metadata(value)
        if separator:
            project_urls.append((label.strip(), url.strip()))
    return project_urls


def _check_project_url_metadata(
    artifact: Path,
    metadata,
    *,
    artifact_label: str,
    metadata_label: str,
) -> list[str]:
    errors: list[str] = []
    project_url_labels: list[str] = []
    for value in metadata.get_all("Project-URL") or []:
        label, separator, url = _split_project_url_metadata(value)
        normalized_label = label.strip()
        normalized_url = url.strip()
        if not separator or not normalized_label or not normalized_url:
            errors.append(
                f"{artifact}: {artifact_label} {metadata_label} "
                f"malformed project URL metadata {value!r}"
            )
        elif not _is_valid_project_url(normalized_url):
            errors.append(
                f"{artifact}: {artifact_label} {metadata_label} "
                f"invalid project URL metadata {value!r}"
            )
        elif not _is_valid_project_url_label(normalized_label):
            errors.append(
                f"{artifact}: {artifact_label} {metadata_label} "
                f"invalid project URL label metadata {normalized_label!r}"
            )
        else:
            project_url_labels.append(normalized_label)
    errors.extend(
        f"{artifact}: {artifact_label} {metadata_label} duplicate project URL {label}"
        for label, count in sorted(Counter(project_url_labels).items())
        if count > 1
    )
    errors.extend(
        f"{artifact}: {artifact_label} {metadata_label} " f"duplicate project URL label {label}"
        for label, count in sorted(
            Counter(_normalize_project_url_label(label) for label in project_url_labels).items()
        )
        if count > 1
    )
    return errors


def _split_project_url_metadata(value: str) -> tuple[str, str, str]:
    return value.rpartition(",")


def _is_valid_project_url(url: str) -> bool:
    parsed = urlsplit(url)
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.netloc)
        and not any(character.isspace() for character in url)
    )


def _is_valid_project_url_label(label: str) -> bool:
    return bool(label) and label.strip() == label and len(label) <= 32 and "," not in label


def _is_valid_pyproject_keyword(keyword: str) -> bool:
    return bool(keyword) and keyword.strip() == keyword and "," not in keyword


def _is_valid_pyproject_classifier(classifier: str) -> bool:
    return bool(classifier) and classifier.strip() == classifier


def _is_valid_pyproject_dynamic_field(field: str) -> bool:
    return bool(field) and field.strip() == field


def _is_valid_pyproject_file_path(path: str) -> bool:
    return (
        bool(path)
        and path.strip() == path
        and path.strip("/") == path
        and not _is_unsafe_archive_name(path, strip_root=False)
    )


def _is_valid_pyproject_script_target(target: str) -> bool:
    return bool(target) and target.strip() == target


def _normalize_project_url_label(label: str) -> str:
    return label.strip().lower()


def _is_valid_author_email(email: str) -> bool:
    local, separator, domain = email.partition("@")
    return (
        bool(local)
        and separator == "@"
        and bool(domain)
        and not any(character.isspace() for character in email)
    )


def _is_valid_pyproject_author_name(name: str) -> bool:
    return bool(name) and name.strip() == name


def _metadata_keywords(metadata) -> list[str]:
    keywords = metadata.get("Keywords")
    if not keywords:
        return []
    return [keyword.strip() for keyword in keywords.split(",") if keyword.strip()]


def _pyproject_author_names(authors: list) -> str | None:
    names = [
        author["name"]
        for author in authors
        if isinstance(author, dict)
        and isinstance(author.get("name"), str)
        and "email" not in author
    ]
    if not names:
        return None
    return ", ".join(names)


def _pyproject_author_email(authors: list) -> str | None:
    email_values: list[str] = []
    for author in authors:
        if not isinstance(author, dict) or not isinstance(author.get("email"), str):
            continue
        email_value = _format_author_email(author)
        if email_value is not None:
            email_values.append(email_value)
    if not email_values:
        return None
    return ", ".join(email_values)


def _pyproject_author_identity(author: dict) -> str | None:
    formatted_email = _format_author_email(author)
    if formatted_email is not None:
        return formatted_email
    name = author.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return None


def _normalize_author_identity(identity: str) -> str:
    return identity.strip().lower()


def _format_author_email(author: dict) -> str | None:
    email = author.get("email")
    if not isinstance(email, str) or not _is_valid_author_email(email):
        return None
    name = author.get("name")
    if isinstance(name, str) and name.strip():
        return f"{name.strip()} <{email}>"
    return email


def _pyproject_readme_content_type(readme) -> str | None:
    if not isinstance(readme, str):
        return None
    suffix = Path(readme).suffix.lower()
    if suffix == ".md":
        return "text/markdown"
    if suffix == ".rst":
        return "text/x-rst"
    if suffix == ".txt":
        return "text/plain"
    return None


def _check_singleton_core_metadata_fields(
    artifact: Path,
    metadata,
    *,
    artifact_label: str,
    metadata_label: str,
) -> list[str]:
    return [
        f"{artifact}: {artifact_label} {metadata_label} duplicate {field} metadata field"
        for field in SINGLETON_CORE_METADATA_FIELDS
        if len(metadata.get_all(field) or []) > 1
    ]


def _check_license_file_metadata(
    artifact: Path,
    metadata,
    *,
    artifact_label: str,
    metadata_label: str,
) -> list[str]:
    metadata_license_file_values = metadata.get_all("License-File") or []
    errors = [
        f"{artifact}: {artifact_label} {metadata_label} duplicate license file metadata {path}"
        for path, count in sorted(Counter(metadata_license_file_values).items())
        if count > 1
    ]
    errors.extend(
        f"{artifact}: {artifact_label} {metadata_label} unsafe license file metadata {path}"
        for path in sorted(set(metadata_license_file_values))
        if _is_unsafe_archive_name(path, strip_root=False)
    )
    return errors


def _check_license_expression_metadata(
    artifact: Path,
    metadata,
    *,
    artifact_label: str,
    metadata_label: str,
) -> list[str]:
    license_expression = metadata.get("License-Expression")
    if license_expression is None:
        return []
    try:
        canonicalize_license_expression(license_expression)
    except InvalidLicenseExpression:
        return [
            f"{artifact}: {artifact_label} {metadata_label} "
            f"invalid License-Expression {license_expression!r}"
        ]
    return []


def _check_core_metadata_version(
    artifact: Path,
    metadata,
    *,
    artifact_label: str,
    metadata_label: str,
) -> list[str]:
    errors: list[str] = []
    metadata_version = metadata.get("Metadata-Version")
    if metadata_version is None:
        return [f"{artifact}: {artifact_label} {metadata_label} missing Metadata-Version"]
    if metadata_version not in {"2.1", "2.2", "2.3", "2.4"}:
        return [
            f"{artifact}: {artifact_label} {metadata_label} unsupported "
            f"Metadata-Version {metadata_version}"
        ]
    if metadata_version == "2.1" and metadata.get_all("Dynamic"):
        errors.append(
            f"{artifact}: {artifact_label} {metadata_label} Dynamic "
            f"requires Metadata-Version 2.2"
        )
    if metadata_version != "2.4":
        errors.extend(
            f"{artifact}: {artifact_label} {metadata_label} {field} "
            f"requires Metadata-Version 2.4"
            for field in ("License-Expression", "License-File")
            if metadata.get_all(field)
        )
    return errors


def _requirement_signature(
    requirement: str,
) -> RequirementSignature | None:
    try:
        parsed = Requirement(requirement)
    except InvalidRequirement:
        return None
    name = _normalize_distribution_name(parsed.name)
    extras = tuple(
        sorted(_normalize_distribution_name(extra.strip()) for extra in parsed.extras if extra)
    )
    specifiers = tuple(sorted(str(specifier) for specifier in parsed.specifier))
    marker = _requirement_marker_signature(str(parsed.marker or ""))
    return (name, extras, specifiers, marker)


def _requirement_direct_url_name(requirement: str) -> str | None:
    try:
        parsed = Requirement(requirement)
    except InvalidRequirement:
        return None
    if parsed.url is None:
        return None
    return _normalize_distribution_name(parsed.name)


def _requirement_marker_signature(marker: str) -> str:
    marker = re.sub(
        r"\(?\s*\bextra\s*==\s*['\"][^'\"]+['\"]\s*\)?",
        "",
        marker,
        flags=re.IGNORECASE,
    ).strip()
    marker = re.sub(r"^(and|or)\s+", "", marker, flags=re.IGNORECASE)
    marker = re.sub(r"\s+(and|or)$", "", marker, flags=re.IGNORECASE)
    marker = re.sub(r"\s+", " ", marker.replace("'", '"').strip().lower())
    marker = re.sub(r"\s*(==|!=|<=|>=|<|>|~=)\s*", r"\1", marker)
    return marker


def _requires_dist_extra(requirement: str) -> str | None:
    extra = _requires_dist_extra_value(requirement)
    if extra is None:
        return None
    return _normalize_distribution_name(extra)


def _requires_dist_extra_value(requirement: str) -> str | None:
    marker = requirement.split(";", 1)[1] if ";" in requirement else ""
    match = re.search(r"\bextra\s*==\s*['\"]([^'\"]+)['\"]", marker)
    return match.group(1) if match is not None else None


def _invalid_requires_dist_extra_markers(requirements: Sequence[str]) -> list[str]:
    return [
        extra
        for requirement in requirements
        for extra in [_requires_dist_extra_value(requirement)]
        if extra is not None and not _is_valid_extra_name(extra)
    ]


def _format_requirement_signature(
    signature: tuple[str, tuple[str, ...], tuple[str, ...], str],
) -> str:
    name, extras, specifiers, marker = signature
    extras_text = f"[{','.join(extras)}]" if extras else ""
    specifiers_text = ",".join(specifiers)
    marker_text = f";{marker}" if marker else ""
    return f"{name}{extras_text}{specifiers_text}{marker_text}"


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
    errors = _check_singleton_wheel_metadata_fields(artifact, metadata)
    if metadata.get("Wheel-Version") != "1.0":
        errors.append(f"{artifact}: wheel WHEEL missing Wheel-Version: 1.0")
    if (metadata.get("Root-Is-Purelib") or "").lower() != "true":
        errors.append(f"{artifact}: wheel WHEEL Root-Is-Purelib must be true")
    wheel_tags = metadata.get_all("Tag") or []
    errors.extend(
        f"{artifact}: wheel WHEEL duplicate Tag metadata {tag}"
        for tag, count in sorted(Counter(wheel_tags).items())
        if count > 1
    )
    errors.extend(
        f"{artifact}: wheel WHEEL unexpected Tag metadata {tag}"
        for tag in sorted(set(wheel_tags) - {filename_tag})
    )
    if filename_tag not in wheel_tags:
        wheel_tag_text = ", ".join(wheel_tags) if wheel_tags else "<missing>"
        errors.append(
            f"{artifact}: wheel tag mismatch: filename {filename_tag}, WHEEL {wheel_tag_text}"
        )
    return errors


def _check_singleton_wheel_metadata_fields(artifact: Path, metadata) -> list[str]:
    return [
        f"{artifact}: wheel WHEEL duplicate {field} metadata field"
        for field in SINGLETON_WHEEL_METADATA_FIELDS
        if len(metadata.get_all(field) or []) > 1
    ]


def _wheel_filename_tag(artifact: Path) -> str | None:
    if artifact.suffix != ".whl":
        return None
    parts = artifact.name.removesuffix(".whl").rsplit("-", 3)
    if len(parts) != 4:
        return None
    return "-".join(parts[1:])


def _check_wheel_entry_points(artifact: Path, entry_points_name: str) -> list[str]:
    parser = _entry_points_parser()
    try:
        parser.read_string(_artifact_text(artifact, entry_points_name))
    except configparser.Error as exc:
        return [f"{artifact}: invalid wheel entry_points.txt: {exc}"]
    script_name, script_target = REQUIRED_CONSOLE_SCRIPT
    errors: list[str] = []
    if not parser.has_section("console_scripts"):
        return [f"{artifact}: missing console script entry point {script_name} = {script_target}"]
    if parser.get("console_scripts", script_name, fallback=None) != script_target:
        errors.append(
            f"{artifact}: missing console script entry point {script_name} = {script_target}"
        )
    errors.extend(
        f"{artifact}: unexpected entry point section {section}"
        for section in sorted(set(parser.sections()) - {"console_scripts"})
    )
    errors.extend(
        f"{artifact}: unexpected console script entry point {name} = {target}"
        for name, target in sorted(parser.items("console_scripts"))
        if name != script_name
    )
    return errors


class _CaseSensitiveConfigParser(configparser.ConfigParser):
    def optionxform(self, optionstr: str) -> str:
        return optionstr


def _entry_points_parser() -> configparser.ConfigParser:
    return _CaseSensitiveConfigParser()


def _check_wheel_top_level(artifact: Path, top_level_name: str) -> list[str]:
    top_level_package_items = [
        line.strip()
        for line in _artifact_text(artifact, top_level_name).splitlines()
        if line.strip()
    ]
    top_level_packages = set(top_level_package_items)
    errors: list[str] = []
    errors.extend(
        f"{artifact}: wheel top_level.txt duplicate top-level package {package}"
        for package, count in sorted(Counter(top_level_package_items).items())
        if count > 1
    )
    if REQUIRED_TOP_LEVEL_PACKAGE not in top_level_packages:
        errors.append(
            f"{artifact}: wheel top_level.txt missing required top-level "
            f"package {REQUIRED_TOP_LEVEL_PACKAGE}"
        )
    errors.extend(
        f"{artifact}: wheel top_level.txt unexpected top-level package {package}"
        for package in sorted(top_level_packages - {REQUIRED_TOP_LEVEL_PACKAGE})
    )
    return errors


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
    if not size.isdecimal():
        return [f"{artifact}: invalid wheel RECORD size for {record_path}: {size}"]
    algorithm, separator, digest_value = digest.partition("=")
    if separator != "=" or algorithm != "sha256":
        return [f"{artifact}: invalid wheel RECORD hash algorithm for {record_path}: {algorithm}"]
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", digest_value):
        return [f"{artifact}: invalid wheel RECORD hash value for {record_path}: {digest_value}"]
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
    errors.extend(_check_pyproject_build_system(artifact, pyproject))
    artifact_identity = _artifact_identity(artifact)
    project = pyproject.get("project")
    errors.extend(_check_sdist_pyproject_identity_fields(artifact, project, artifact_identity))
    errors.extend(_check_sdist_pyproject_requires_python(artifact, project))
    errors.extend(_check_sdist_pyproject_dependencies(artifact, project))
    errors.extend(_check_sdist_pyproject_scripts(artifact, project))
    return errors


def _check_sdist_pyproject_identity_fields(
    artifact: Path,
    project: object,
    artifact_identity: tuple[str, str] | None,
) -> list[str]:
    name = project.get("name") if isinstance(project, dict) else None
    version = project.get("version") if isinstance(project, dict) else None
    if not isinstance(project, dict) or name is None or version is None:
        return [f"{artifact}: sdist pyproject.toml missing project name or version"]
    if not isinstance(name, str):
        return [f"{artifact}: pyproject invalid project.name {name!r}"]
    if not isinstance(version, str):
        return [f"{artifact}: pyproject invalid project.version {version!r}"]
    if not _is_valid_distribution_name(name):
        return [f"{artifact}: pyproject invalid project.name {name!r}"]
    if not _is_valid_version(version):
        return [f"{artifact}: pyproject invalid project.version {version!r}"]

    pyproject_identity = _pyproject_identity(project)
    if (
        artifact_identity is not None
        and pyproject_identity is not None
        and pyproject_identity != artifact_identity
    ):
        return [
            f"{artifact}: sdist pyproject.toml metadata mismatch: filename "
            f"{artifact_identity[0]} {artifact_identity[1]}, "
            f"pyproject {pyproject_identity[0]} {pyproject_identity[1]}"
        ]
    return []


def _check_sdist_pyproject_requires_python(artifact: Path, project: object) -> list[str]:
    requires_python = project.get("requires-python") if isinstance(project, dict) else None
    if not isinstance(project, dict) or requires_python is None:
        return [f"{artifact}: sdist pyproject.toml missing project.requires-python"]
    if not isinstance(requires_python, str):
        return [f"{artifact}: pyproject invalid requires-python {requires_python!r}"]
    if not _is_valid_python_specifier(requires_python):
        return [f"{artifact}: pyproject invalid requires-python {requires_python!r}"]
    return []


def _check_sdist_pyproject_dependencies(artifact: Path, project: object) -> list[str]:
    dependencies = project.get("dependencies") if isinstance(project, dict) else None
    if not isinstance(project, dict) or dependencies is None:
        return [f"{artifact}: sdist pyproject.toml missing project.dependencies list"]
    if not isinstance(dependencies, list):
        return [f"{artifact}: pyproject invalid dependencies list {dependencies!r}"]
    return []


def _check_sdist_pyproject_scripts(artifact: Path, project: object) -> list[str]:
    script_name, script_target = REQUIRED_CONSOLE_SCRIPT
    scripts = project.get("scripts") if isinstance(project, dict) else None
    errors: list[str] = []
    if not isinstance(project, dict) or scripts is None:
        errors.append(
            f"{artifact}: sdist pyproject.toml missing project.scripts."
            f"{script_name} = {script_target}"
        )
    elif not isinstance(scripts, dict):
        errors.append(f"{artifact}: pyproject invalid project.scripts table {scripts!r}")
    elif script_name not in scripts:
        errors.append(
            f"{artifact}: sdist pyproject.toml missing project.scripts."
            f"{script_name} = {script_target}"
        )
    elif not isinstance(scripts[script_name], str) or not _is_valid_pyproject_script_target(
        scripts[script_name]
    ):
        errors.append(
            f"{artifact}: pyproject invalid project.scripts."
            f"{script_name} {scripts[script_name]!r}"
        )
    elif scripts[script_name] != script_target:
        errors.append(
            f"{artifact}: sdist pyproject.toml missing project.scripts."
            f"{script_name} = {script_target}"
        )
    if isinstance(scripts, dict):
        errors.extend(
            f"{artifact}: pyproject invalid project.scripts.{name} {target!r}"
            for name, target in sorted(scripts.items())
            if not isinstance(target, str)
        )
        errors.extend(
            f"{artifact}: pyproject unexpected project.scripts.{name} = {target}"
            for name, target in sorted(scripts.items())
            if name != script_name and isinstance(target, str)
        )
    return errors


def _check_pyproject_build_system(artifact: Path, pyproject: dict) -> list[str]:
    build_system = pyproject.get("build-system")
    if build_system is None:
        return []
    if not isinstance(build_system, dict):
        return [f"{artifact}: pyproject invalid build-system table {build_system!r}"]
    errors: list[str] = []
    errors.extend(
        f"{artifact}: pyproject unsupported build-system field {field}"
        for field in sorted(build_system)
        if field not in SUPPORTED_BUILD_SYSTEM_FIELDS
    )
    requires = build_system.get("requires")
    if requires != list(REQUIRED_BUILD_SYSTEM_REQUIRES):
        errors.append(f"{artifact}: pyproject unexpected build-system.requires {requires!r}")
    build_backend = build_system.get("build-backend")
    if build_backend != REQUIRED_BUILD_BACKEND:
        errors.append(f"{artifact}: pyproject unexpected build-backend {build_backend!r}")
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
