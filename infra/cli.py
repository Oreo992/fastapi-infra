from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Mapping, Sequence

from infra.config import load_env_file, load_infra_settings, validate_infra_settings
from infra.config.models import InfraSettings
from infra.database.migrations import (
    MigrationDatabase,
    MigrationError,
    SqlMigrationRunner,
    create_sql_migration,
    load_sql_migrations,
)
from infra.provider_certification import (
    DEFAULT_LIVE_PROVIDER_TEST_PATH,
    format_provider_checks,
    format_provider_env_template,
    get_provider_checks,
    run_provider_preflight,
    run_pytest_certification,
    selected_checks,
)


def get_available_plugins(settings: InfraSettings | None = None):
    from infra.plugins.discovery import get_available_plugins as load_available_plugins

    return load_available_plugins(settings)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "new":
        return _run_new(args)
    if args.command == "migrations":
        return _run_migrations(args)
    if args.command == "certify-providers":
        return _run_certify_providers(args)
    if args.command == "release-check":
        return _run_release_check(args)
    if args.command == "project-check":
        return _run_project_check(args)
    if args.command == "plugins":
        return _run_plugins(args)
    if args.command == "profiles":
        return _run_profiles(args)
    if args.command == "config-check":
        return _run_config_check(args)

    parser.print_help(sys.stderr)
    return 2


def _build_parser() -> argparse.ArgumentParser:
    from infra.plugins.template import SUPPORTED_PROVIDER_KINDS

    parser = argparse.ArgumentParser(prog="fastapi-infra")
    subparsers = parser.add_subparsers(dest="command")

    new_parser = subparsers.add_parser("new", help="create a FastAPI project")
    new_parser.add_argument("path", type=Path, help="project destination")
    new_parser.add_argument(
        "--profile",
        default="minimal",
        help="plugin profile to use; run fastapi-infra profiles to list choices",
    )
    new_parser.add_argument(
        "--plugins",
        default="",
        help="comma-separated plugins to add on top of --profile",
    )
    new_parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite generated files in a non-empty destination",
    )

    migrations_parser = subparsers.add_parser("migrations", help="manage SQL migrations")
    migration_subparsers = migrations_parser.add_subparsers(dest="migration_command")

    migration_new = migration_subparsers.add_parser("new", help="create a SQL migration file")
    migration_new.add_argument("path", type=Path, help="migrations directory")
    migration_new.add_argument("name", help="migration name")

    migration_list = migration_subparsers.add_parser("list", help="list SQL migration files")
    migration_list.add_argument("path", type=Path, help="migrations directory")

    migration_migrate = migration_subparsers.add_parser(
        "migrate",
        help="apply pending SQL migrations through the configured database plugin",
    )
    migration_migrate.add_argument("path", type=Path, help="migrations directory")
    migration_migrate.add_argument(
        "--settings",
        type=Path,
        required=True,
        help="JSON or TOML InfraSettings file with the database plugin enabled",
    )

    certify_parser = subparsers.add_parser(
        "certify-providers",
        help="run opt-in live provider certification checks",
    )
    certify_parser.add_argument(
        "--provider",
        action="append",
        default=[],
        help="provider check to require; repeat for multiple providers",
    )
    certify_parser.add_argument(
        "--settings",
        type=Path,
        help="select provider checks required by a JSON or TOML InfraSettings file",
    )
    certify_parser.add_argument(
        "--settings-env-file",
        type=Path,
        help="load runtime environment variables before resolving settings $env references",
    )
    certify_parser.add_argument(
        "--test-path",
        help=(
            "pytest path containing live provider tests; defaults to each provider "
            "check's declared test_path"
        ),
    )
    certify_parser.add_argument(
        "--json",
        action="store_true",
        help="print a machine-readable certification report",
    )
    certify_parser.add_argument(
        "--list",
        action="store_true",
        help="list known provider checks and their required live tests",
    )
    certify_parser.add_argument(
        "--env-template",
        action="store_true",
        help="print a .env template for selected live provider checks",
    )
    certify_parser.add_argument(
        "--env-file",
        type=Path,
        help="load provider certification environment variables from a .env file",
    )
    certify_parser.add_argument(
        "--preflight",
        action="store_true",
        help="check required environment variables and packages without running live tests",
    )
    certify_parser.add_argument(
        "--requirements",
        action="store_true",
        help="include required env vars, optional env vars, and packages in --list output",
    )

    release_parser = subparsers.add_parser(
        "release-check",
        help="validate production-readiness configuration without starting the app",
    )
    release_parser.add_argument(
        "--settings",
        type=Path,
        required=True,
        help="JSON or TOML InfraSettings file to validate",
    )
    release_parser.add_argument(
        "--json",
        action="store_true",
        help="print a machine-readable release readiness report",
    )
    release_parser.add_argument(
        "--provider-certification-report",
        type=Path,
        help="JSON report produced by certify-providers --json",
    )
    release_parser.add_argument(
        "--static-only",
        action="store_true",
        help="skip live provider certification requirements and only run static checks",
    )
    release_parser.add_argument(
        "--env-file",
        type=Path,
        help="load environment variables before resolving settings $env references",
    )
    release_parser.add_argument(
        "--migrations",
        type=Path,
        help="validate enabled plugin schema migrations in this directory",
    )

    project_check_parser = subparsers.add_parser(
        "project-check",
        help="audit a generated project manifest against its files and configs",
    )
    project_check_parser.add_argument(
        "path",
        type=Path,
        help="generated project directory",
    )
    project_check_parser.add_argument(
        "--json",
        action="store_true",
        help="print a machine-readable generated project audit report",
    )

    plugins_parser = subparsers.add_parser(
        "plugins",
        help="list available plugins and their service/config manifest",
    )
    plugins_parser.add_argument(
        "--json",
        action="store_true",
        help="print a machine-readable plugin manifest",
    )
    plugins_parser.add_argument(
        "--settings",
        type=Path,
        help="JSON or TOML InfraSettings file used to show configured plugin state",
    )
    plugins_parser.add_argument(
        "--lifecycle",
        action="store_true",
        help="with 'plugins check', start plugins and run health checks",
    )
    plugins_parser.add_argument(
        "--force",
        action="store_true",
        help="with 'plugins init', overwrite files in a non-empty destination",
    )
    plugins_parser.add_argument(
        "--kind",
        choices=("service", "provider"),
        default="service",
        help="with 'plugins init', choose a full service plugin or provider adapter template",
    )
    plugins_parser.add_argument(
        "--provider-kind",
        choices=tuple(sorted(SUPPORTED_PROVIDER_KINDS)),
        default="ai",
        help="with 'plugins init --kind provider', choose the provider registry to target",
    )
    plugins_parser.add_argument(
        "plugin_command",
        nargs="?",
        choices=("check", "init"),
        help="optional plugin subcommand",
    )
    plugins_parser.add_argument(
        "plugin_names",
        nargs="*",
        help="plugin names used by 'plugins check'",
    )

    profiles_parser = subparsers.add_parser(
        "profiles",
        help="list scaffold plugin profiles",
    )
    profiles_parser.add_argument(
        "--json",
        action="store_true",
        help="print machine-readable scaffold profiles",
    )

    config_check_parser = subparsers.add_parser(
        "config-check",
        help="validate plugin configuration schemas without starting the app",
    )
    config_check_parser.add_argument(
        "--settings",
        type=Path,
        required=True,
        help="JSON or TOML InfraSettings file to validate",
    )
    config_check_parser.add_argument(
        "--json",
        action="store_true",
        help="print a machine-readable config validation report",
    )
    config_check_parser.add_argument(
        "--env-file",
        type=Path,
        help="load environment variables before resolving settings $env references",
    )

    return parser


def _run_new(args: argparse.Namespace) -> int:
    from infra.plugins.builtin import get_builtin_plugins
    from infra.plugins.discovery import load_entry_point_plugins
    from infra.plugins.manager import PluginDependencyError
    from infra.scaffold import create_project, plugins_for_profile

    destination = args.path
    project_name = destination.name

    try:
        extra_plugins = _parse_plugins(args.plugins)
        profile_plugins = plugins_for_profile(args.profile)
        builtin_plugins = get_builtin_plugins()
        builtin_names = {plugin.metadata.name for plugin in builtin_plugins}
        external_names = set(extra_plugins) - builtin_names
        external_plugins = load_entry_point_plugins(configured_names=external_names)
        plugin_registry = [*builtin_plugins, *external_plugins]
        enabled_plugins = tuple(dict.fromkeys((*profile_plugins, *extra_plugins)))
        written = create_project(
            destination,
            project_name,
            enabled_plugins=extra_plugins,
            profile=args.profile,
            overwrite=args.force,
            plugin_registry=plugin_registry,
        )
    except (FileExistsError, PluginDependencyError, ValueError) as exc:
        print(f"fastapi-infra: error: {exc}", file=sys.stderr)
        return 1

    print(f"Created {destination} ({len(written)} files)")
    print(f"Profile: {args.profile.strip() or 'minimal'}")
    print(f"Plugins: {', '.join(enabled_plugins) or 'none'}")
    print("Next steps:")
    print(f"  cd {destination}")
    print('  pip install -e ".[dev]"')
    print("  fastapi-infra config-check --settings infra.toml")
    print("  fastapi-infra project-check .")
    print("  python -m pytest -q")
    print("  uvicorn app.main:app --reload")
    print("Release checks:")
    print("  make env")
    print("  scripts/verify-release.sh .env provider.env")
    return 0


def _parse_plugins(value: str) -> tuple[str, ...]:
    if not value.strip():
        return ()
    return tuple(plugin.strip() for plugin in value.split(",") if plugin.strip())


def _run_plugins(args: argparse.Namespace) -> int:
    from infra.plugins.manager import PluginManager

    if args.plugin_command == "init":
        return _run_plugins_init(args)
    settings = _load_optional_settings(args.settings)
    if settings is None:
        return 1
    if args.plugin_command == "check":
        return _run_plugins_check(args, settings)
    manager = PluginManager(settings=settings, plugins=get_available_plugins(settings))
    manifest = manager.manifest()
    if args.json:
        print(json.dumps(manifest, indent=2, sort_keys=True))
        return 0
    for name, item in manifest.items():
        state = "enabled" if item["default_enabled"] else "disabled"
        provided_services = item["provides"]
        provides = (
            ",".join(provided_services)
            if isinstance(provided_services, list)
            and all(isinstance(service, str) for service in provided_services)
            else "-"
        )
        line = f"{name} {state} provides={provides}"
        service_name_config = item.get("service_name_config")
        if service_name_config is not None:
            line += f" service_name_config={service_name_config}"
        configured_services = item.get("configured_services")
        if (
            isinstance(configured_services, list)
            and configured_services != provided_services
            and all(isinstance(service, str) for service in configured_services)
        ):
            line += f" configured_services={','.join(configured_services)}"
        service_keys = item.get("service_keys")
        if isinstance(service_keys, Mapping):
            entries = [
                f"{service}:{import_path}"
                for service, import_path in sorted(service_keys.items())
                if isinstance(service, str) and isinstance(import_path, str)
            ]
            if entries:
                line += f" service_keys={','.join(entries)}"
        print(line)
    return 0


def _run_project_check(args: argparse.Namespace) -> int:
    from infra.project_check import (
        build_project_check_report,
        format_project_check_json,
        format_project_check_text,
    )

    report = build_project_check_report(args.path)
    if args.json:
        print(format_project_check_json(report))
    else:
        print(format_project_check_text(report))
    return 0 if report["valid"] else 1


def _load_optional_settings(path: Path | None) -> InfraSettings | None:
    if path is None:
        return InfraSettings()
    if not path.exists():
        print(
            f"fastapi-infra: error: settings file not found: {path}",
            file=sys.stderr,
        )
        return None
    try:
        return load_infra_settings(path)
    except (OSError, ValueError) as exc:
        print(f"fastapi-infra: error: {exc}", file=sys.stderr)
        return None


def _run_plugins_check(args: argparse.Namespace, settings: InfraSettings) -> int:
    from infra.plugins.conformance import check_plugins_conformance, conformance_report

    try:
        plugins = _plugins_for_check(settings, tuple(args.plugin_names))
    except ValueError as exc:
        print(f"fastapi-infra: error: {exc}", file=sys.stderr)
        return 1
    results = check_plugins_conformance(
        plugins,
        settings=settings if args.settings is not None else None,
        lifecycle=args.lifecycle,
    )
    report = conformance_report(results)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print("plugins check: valid" if report["valid"] else "plugins check: invalid")
        for result in results:
            if result.valid:
                print(f"- {result.name}: valid")
                continue
            print(f"- {result.name}: invalid")
            for issue in result.issues:
                print(f"  {issue.severity} {issue.code}: {issue.message}")
    return 0 if report["valid"] else 1


def _run_plugins_init(args: argparse.Namespace) -> int:
    from infra.plugins.template import create_plugin_project

    if not args.plugin_names:
        print("fastapi-infra: error: plugins init requires a plugin name", file=sys.stderr)
        return 2
    if len(args.plugin_names) > 2:
        print(
            "fastapi-infra: error: plugins init accepts at most a plugin name and destination",
            file=sys.stderr,
        )
        return 2

    plugin_name = args.plugin_names[0]
    destination = (
        Path(args.plugin_names[1])
        if len(args.plugin_names) == 2
        else Path(f"fastapi-infra-{plugin_name.replace('_', '-')}-plugin")
    )
    try:
        written = create_plugin_project(
            destination,
            plugin_name,
            kind=args.kind,
            provider_kind=args.provider_kind,
            overwrite=args.force,
        )
    except (FileExistsError, ValueError) as exc:
        print(f"fastapi-infra: error: {exc}", file=sys.stderr)
        return 1

    template_label = "provider template" if args.kind == "provider" else "plugin template"
    print(f"Created {template_label} {destination} ({len(written)} files)")
    print("Next steps:")
    print(f"  cd {destination}")
    print('  pip install -e ".[dev]"')
    print("  python -m pytest -q")
    if args.kind == "provider":
        print("  fastapi-infra config-check --settings infra.example.toml")
        provider_plugin = _plugin_for_provider_kind(args.provider_kind)
        print(f"  fastapi-infra new /tmp/{plugin_name}-api --plugins {provider_plugin}")
    else:
        print(
            f"  fastapi-infra plugins check {plugin_name} --settings infra.example.toml --lifecycle"
        )
        print(f"  fastapi-infra new /tmp/{plugin_name}-api --plugins {plugin_name}")
    return 0


def _plugin_for_provider_kind(provider_kind: str) -> str:
    if provider_kind == "webhook":
        return "webhooks"
    return provider_kind


def _plugins_for_check(
    settings: InfraSettings,
    plugin_names: tuple[str, ...],
):
    from infra.plugins.builtin import get_builtin_plugins
    from infra.plugins.discovery import load_entry_point_plugins

    builtin_plugins = get_builtin_plugins()
    if not plugin_names:
        return get_available_plugins(settings)
    builtin_by_name = {plugin.metadata.name: plugin for plugin in builtin_plugins}
    requested = tuple(dict.fromkeys(plugin_names))
    external_names = set(requested) - set(builtin_by_name)
    plugins = [builtin_by_name[name] for name in requested if name in builtin_by_name]
    plugins.extend(load_entry_point_plugins(configured_names=external_names))
    loaded_names = {plugin.metadata.name for plugin in plugins}
    missing = sorted(set(requested) - loaded_names)
    if missing:
        raise ValueError(
            "unknown plugin name: "
            + ", ".join(missing)
            + ". Run fastapi-infra plugins to list plugin metadata."
        )
    return plugins


def _run_profiles(args: argparse.Namespace) -> int:
    from infra.scaffold import plugin_profile_descriptions, plugin_profiles

    profiles = plugin_profiles()
    descriptions = plugin_profile_descriptions()
    if args.json:
        payload = {
            name: {
                "plugins": list(plugins),
                "description": descriptions.get(name, ""),
            }
            for name, plugins in profiles.items()
        }
        print(json.dumps(payload, indent=2))
        return 0
    for name, plugins in profiles.items():
        plugin_list = ", ".join(plugins) or "none"
        description = descriptions.get(name, "")
        suffix = f" - {description}" if description else ""
        print(f"{name}: {plugin_list}{suffix}")
    return 0


def _run_config_check(args: argparse.Namespace) -> int:
    if not args.settings.exists():
        print(f"fastapi-infra: error: settings file not found: {args.settings}", file=sys.stderr)
        return 1
    should_load_env_file = args.env_file is not None and (
        args.settings is not None or not (args.list or args.env_template)
    )
    try:
        environ = load_env_file(args.env_file) if should_load_env_file else None
        with _patched_environ(environ):
            settings = load_infra_settings(args.settings)
    except (OSError, ValueError) as exc:
        print(f"fastapi-infra: error: {exc}", file=sys.stderr)
        return 1

    issues = validate_infra_settings(settings, get_available_plugins(settings))
    report = {
        "valid": not issues,
        "issues": [asdict(issue) for issue in issues],
    }
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    elif issues:
        print("config-check: invalid")
        for issue in issues:
            print(f"- {issue.plugin}: {issue.code}: {issue.message}")
    else:
        print("config-check: valid")
    return 0 if not issues else 1


def _run_migrations(args: argparse.Namespace) -> int:
    if args.migration_command == "new":
        try:
            path = create_sql_migration(args.path, args.name)
        except (FileExistsError, ValueError) as exc:
            print(f"fastapi-infra: error: {exc}", file=sys.stderr)
            return 1
        print(f"Created migration {path}")
        return 0

    if args.migration_command == "list":
        try:
            migrations = load_sql_migrations(args.path, allow_empty=True)
        except MigrationError as exc:
            print(f"fastapi-infra: error: {exc}", file=sys.stderr)
            return 1
        for migration in migrations:
            print(
                f"{migration.version} {migration.name} {migration.checksum[:12]} {migration.path}"
            )
        return 0

    if args.migration_command == "migrate":
        try:
            applied = asyncio.run(_apply_migrations(args.path, args.settings))
        except (MigrationError, RuntimeError, ValueError) as exc:
            print(f"fastapi-infra: error: {exc}", file=sys.stderr)
            return 1
        if not applied:
            print("No pending migrations")
            return 0
        for migration in applied:
            print(f"Applied {migration.version} {migration.name}")
        return 0

    print("fastapi-infra: error: migrations command required", file=sys.stderr)
    return 2


async def _apply_migrations(migrations_path: Path, settings_path: Path):
    from infra.plugins.manager import PluginManager

    settings = load_infra_settings(settings_path)
    manager = PluginManager(settings=settings, plugins=get_available_plugins())
    await manager.startup()
    try:
        database = manager.get("database")
        if not isinstance(database, MigrationDatabase):
            raise RuntimeError("database plugin is not enabled")
        runner = SqlMigrationRunner(database, migrations_path)
        return await runner.migrate()
    finally:
        await manager.shutdown()


def _run_certify_providers(args: argparse.Namespace) -> int:
    try:
        provider_checks = get_provider_checks()
    except ValueError as exc:
        print(f"fastapi-infra: error: {exc}", file=sys.stderr)
        return 2
    should_load_env_file = args.env_file is not None and (not (args.list or args.env_template))
    try:
        provider_environ = load_env_file(args.env_file) if should_load_env_file else None
        settings_environ = (
            load_env_file(args.settings_env_file) if args.settings_env_file is not None else None
        )
    except ValueError as exc:
        print(f"fastapi-infra: error: {exc}", file=sys.stderr)
        return 1

    provider_names = list(args.provider)
    if args.settings is not None:
        if not args.settings.exists():
            print(
                f"fastapi-infra: error: settings file not found: {args.settings}",
                file=sys.stderr,
            )
            return 1
        from infra.release_check import expected_provider_check_names

        try:
            with _patched_environ(settings_environ):
                settings = load_infra_settings(
                    args.settings,
                    missing_env="error" if settings_environ is not None else "placeholder",
                )
            provider_names.extend(
                expected_provider_check_names(
                    settings,
                    plugins=get_available_plugins(settings),
                    provider_checks=provider_checks,
                )
            )
        except (OSError, ValueError) as exc:
            print(f"fastapi-infra: error: {exc}", file=sys.stderr)
            return 1
    try:
        checks = (
            selected_checks(provider_names, checks=provider_checks)
            if provider_names or args.settings is None
            else ()
        )
    except (SystemExit, ValueError) as exc:
        print(f"fastapi-infra: error: {exc}", file=sys.stderr)
        return 2
    if args.env_template:
        print(format_provider_env_template(checks))
        return 0
    if args.list:
        print(
            format_provider_checks(
                checks,
                include_requirements=args.requirements,
            )
        )
        return 0
    if args.preflight:
        return run_provider_preflight(
            checks,
            json_output=args.json,
            environ=provider_environ,
        )
    return run_pytest_certification(
        args.test_path,
        checks,
        json_output=args.json,
        environ=provider_environ,
    )


def _run_release_check(args: argparse.Namespace) -> int:
    from infra.release_check import (
        build_release_check_report,
        format_release_check_json,
        format_release_check_text_report,
    )

    if not args.settings.exists():
        print(f"fastapi-infra: error: settings file not found: {args.settings}", file=sys.stderr)
        return 1
    try:
        environ = load_env_file(args.env_file) if args.env_file is not None else None
        with _patched_environ(environ):
            settings = load_infra_settings(args.settings)
    except (OSError, ValueError) as exc:
        print(f"fastapi-infra: error: {exc}", file=sys.stderr)
        return 1
    provider_report = None
    if args.provider_certification_report is not None:
        try:
            provider_report = _load_json_report(args.provider_certification_report)
        except (OSError, ValueError) as exc:
            print(f"fastapi-infra: error: {exc}", file=sys.stderr)
            return 1
    plugins = get_available_plugins(settings)
    provider_checks = get_provider_checks()
    report = build_release_check_report(
        settings,
        provider_certification_report=provider_report,
        require_provider_certification=not args.static_only,
        plugins=plugins,
        provider_checks=provider_checks,
        migrations_path=args.migrations,
    )

    if args.json:
        print(format_release_check_json(report))
    else:
        print(format_release_check_text_report(report))
    return 0 if report["ready"] else 1


@contextmanager
def _patched_environ(environ: Mapping[str, str] | None):
    if environ is None:
        yield
        return
    original = dict(os.environ)
    os.environ.clear()
    os.environ.update(environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(original)


def _load_json_report(path: Path) -> dict[str, object]:
    if not path.exists():
        raise ValueError(f"provider certification report not found: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid provider certification report JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"provider certification report must be a JSON object: {path}")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
