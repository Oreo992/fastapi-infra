import json
import os
import subprocess
import sys
from pathlib import Path

from infra.cli import main
from infra.config.models import InfraSettings
from infra.core.health import HealthState
from infra.plugins.contract import PluginContext, PluginMetadata
from infra.release_check import ReleaseCheckIssue


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class CLIEntryPointPlugin:
    metadata = PluginMetadata(
        name="external",
        version="1.0.0",
        provides=["external"],
    )
    config_model = None
    manifest_hints = {
        "service_keys": {"external": "example.ExternalService"},
        "env_vars": ["EXTERNAL_API_KEY"],
        "local_config_example": {"mode": "local"},
        "production_config_example": {"mode": "production", "api_key": "${EXTERNAL_API_KEY}"},
        "recommended_extras": ["http"],
        "scaffold_files": [
            {
                "path": "app/external.py",
                "content": "def external_status() -> str:\n    return 'ready'\n",
            }
        ],
        "scaffold_readme_sections": [
            "## External\n\nThis project was extended by an external plugin.\n"
        ],
    }

    def register(self, ctx: PluginContext) -> None:
        ctx.services["external"] = object()

    async def startup(self, ctx: PluginContext) -> None:
        return None

    async def shutdown(self, ctx: PluginContext) -> None:
        return None

    async def health_check(self, ctx: PluginContext):
        return ctx.health_status("external", HealthState.HEALTHY)


class InvalidCLIEntryPointPlugin(CLIEntryPointPlugin):
    manifest_hints = {"service_keys": {"missing": "example.MissingService"}}


class FakeEntryPoint:
    def __init__(self, loaded, name="external"):
        self.name = name
        self._loaded = loaded

    def load(self):
        return self._loaded


def test_new_command_creates_project_with_requested_plugins(tmp_path, capsys):
    destination = tmp_path / "billing_api"

    result = main(
        [
            "new",
            str(destination),
            "--plugins",
            "ai,auth,payment",
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert "Created" in captured.out
    assert f"cd {destination}" in captured.out
    assert 'pip install -e ".[dev]"' in captured.out
    assert "fastapi-infra config-check --settings infra.toml" in captured.out
    assert "fastapi-infra project-check ." in captured.out
    assert (destination / "infra.manifest.json").exists()
    assert "python -m pytest -q" in captured.out
    assert "uvicorn app.main:app --reload" in captured.out
    assert "Release checks:" in captured.out
    assert "make env" in captured.out
    assert "scripts/verify-release.sh .env provider.env" in captured.out
    assert captured.err == ""
    settings_py = read(destination / "app" / "settings.py")
    infra_toml = read(destination / "infra.toml")
    assert "INFRA_SETTINGS" in settings_py
    assert "load_infra_settings(config_path)" in settings_py
    assert "[infra.plugins.ai]\nenabled = true" in infra_toml
    assert "[infra.plugins.auth]\nenabled = true" in infra_toml
    assert "[infra.plugins.payment]\nenabled = true" in infra_toml
    assert "[infra.plugins.tasks]\nenabled = false" in infra_toml
    assert "from infra import InfraSettings" in settings_py
    assert "from infra import InfraSettings, setup_infra" in read(destination / "app" / "main.py")
    assert (destination / "Dockerfile").exists()
    assert (destination / "AGENTS.md").exists()
    assert (destination / "Makefile").exists()
    assert (destination / "compose.yaml").exists()
    assert (destination / ".github" / "workflows" / "ci.yml").exists()
    assert (destination / ".dockerignore").exists()
    assert (destination / ".gitignore").exists()
    assert (destination / "tests" / "test_health.py").exists()


def test_project_check_command_validates_generated_project(tmp_path, capsys):
    destination = tmp_path / "billing_api"
    assert main(["new", str(destination), "--profile", "api", "--plugins", "tasks"]) == 0
    capsys.readouterr()

    result = main(["project-check", str(destination)])

    captured = capsys.readouterr()
    assert result == 0
    assert captured.err == ""
    assert "project-check: valid" in captured.out
    assert "errors: 0" in captured.out
    assert "warnings: 0" in captured.out


def test_project_check_command_reports_manifest_mismatch(tmp_path, capsys):
    destination = tmp_path / "billing_api"
    assert main(["new", str(destination), "--plugins", "auth"]) == 0
    capsys.readouterr()
    manifest = json.loads(read(destination / "infra.manifest.json"))
    manifest["enabled_plugins"] = []
    (destination / "infra.manifest.json").write_text(
        json.dumps(manifest, indent=2),
        encoding="utf-8",
    )

    result = main(["project-check", str(destination), "--json"])

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert result == 1
    assert captured.err == ""
    assert report["valid"] is False
    codes = {issue["code"] for issue in report["issues"]}
    assert "manifest_plugin_requested_mismatch" in codes
    assert "config_plugin_mismatch" in codes


def test_project_check_command_reports_manifest_plugin_summary_mismatch(tmp_path, capsys):
    destination = tmp_path / "billing_api"
    assert main(["new", str(destination), "--plugins", "auth,tasks"]) == 0
    capsys.readouterr()
    manifest = json.loads(read(destination / "infra.manifest.json"))
    manifest["plugins"][1]["requested"] = False
    manifest["plugins"] = manifest["plugins"][1:]
    (destination / "infra.manifest.json").write_text(
        json.dumps(manifest, indent=2),
        encoding="utf-8",
    )

    result = main(["project-check", str(destination), "--json"])

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert result == 1
    assert captured.err == ""
    assert report["valid"] is False
    codes = {issue["code"] for issue in report["issues"]}
    assert "manifest_plugins_mismatch" in codes
    assert "manifest_plugin_requested_mismatch" in codes


def test_project_check_command_reports_pyproject_dependency_mismatch(tmp_path, capsys):
    destination = tmp_path / "billing_api"
    assert main(["new", str(destination), "--profile", "api"]) == 0
    capsys.readouterr()
    pyproject = destination / "pyproject.toml"
    pyproject.write_text(
        read(pyproject).replace(
            '"fastapi-infra[http,mysql,observability,redis]"',
            '"fastapi-infra[http]"',
        ),
        encoding="utf-8",
    )

    result = main(["project-check", str(destination), "--json"])

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert result == 1
    assert captured.err == ""
    assert report["valid"] is False
    assert report["issues"][0]["code"] == "pyproject_dependency_mismatch"
    assert report["issues"][0]["path"] == "pyproject.toml"
    assert "fastapi-infra[http,mysql,observability,redis]" in report["issues"][0]["message"]


def test_project_check_command_reports_manifest_command_mismatch(tmp_path, capsys):
    destination = tmp_path / "billing_api"
    assert main(["new", str(destination), "--plugins", "auth"]) == 0
    capsys.readouterr()
    manifest = json.loads(read(destination / "infra.manifest.json"))
    manifest["commands"]["local_verify"] = ["python -m pytest -q"]
    (destination / "infra.manifest.json").write_text(
        json.dumps(manifest, indent=2),
        encoding="utf-8",
    )

    result = main(["project-check", str(destination), "--json"])

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert result == 1
    assert captured.err == ""
    assert report["valid"] is False
    assert report["issues"][0]["code"] == "manifest_command_mismatch"
    assert report["issues"][0]["path"] == "infra.manifest.json"
    assert "local_verify" in report["issues"][0]["message"]
    assert "make verify" in report["issues"][0]["message"]


def test_project_check_command_reports_missing_ci_gate(tmp_path, capsys):
    destination = tmp_path / "billing_api"
    assert main(["new", str(destination), "--profile", "api"]) == 0
    capsys.readouterr()
    ci_path = destination / ".github" / "workflows" / "ci.yml"
    ci_path.write_text(
        read(ci_path).replace("make verify", "python -m pytest -q"),
        encoding="utf-8",
    )

    result = main(["project-check", str(destination), "--json"])

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert result == 1
    assert captured.err == ""
    assert report["valid"] is False
    assert report["issues"][0]["code"] == "ci_workflow_missing_gate"
    assert report["issues"][0]["path"] == ".github/workflows/ci.yml"
    assert "make verify" in report["issues"][0]["message"]


def test_project_check_command_reports_missing_dockerfile_gate(tmp_path, capsys):
    destination = tmp_path / "billing_api"
    assert main(["new", str(destination), "--plugins", "auth"]) == 0
    capsys.readouterr()
    dockerfile = destination / "Dockerfile"
    dockerfile.write_text(
        read(dockerfile).replace("USER appuser", "USER root"),
        encoding="utf-8",
    )

    result = main(["project-check", str(destination), "--json"])

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert result == 1
    assert captured.err == ""
    assert report["valid"] is False
    assert report["issues"][0]["code"] == "dockerfile_missing_gate"
    assert report["issues"][0]["path"] == "Dockerfile"
    assert "USER appuser" in report["issues"][0]["message"]


def test_project_check_command_reports_missing_dockerignore_gate(tmp_path, capsys):
    destination = tmp_path / "billing_api"
    assert main(["new", str(destination), "--plugins", "auth"]) == 0
    capsys.readouterr()
    dockerignore = destination / ".dockerignore"
    dockerignore.write_text(
        read(dockerignore).replace("provider.env\n", ""),
        encoding="utf-8",
    )

    result = main(["project-check", str(destination), "--json"])

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert result == 1
    assert captured.err == ""
    assert report["valid"] is False
    assert report["issues"][0]["code"] == "dockerignore_missing_gate"
    assert report["issues"][0]["path"] == ".dockerignore"
    assert "provider.env" in report["issues"][0]["message"]


def test_project_check_command_reports_missing_gitignore_gate(tmp_path, capsys):
    destination = tmp_path / "billing_api"
    assert main(["new", str(destination), "--plugins", "auth"]) == 0
    capsys.readouterr()
    gitignore = destination / ".gitignore"
    gitignore.write_text(
        read(gitignore).replace("provider-certification.json\n", ""),
        encoding="utf-8",
    )

    result = main(["project-check", str(destination), "--json"])

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert result == 1
    assert captured.err == ""
    assert report["valid"] is False
    assert report["issues"][0]["code"] == "gitignore_missing_gate"
    assert report["issues"][0]["path"] == ".gitignore"
    assert "provider-certification.json" in report["issues"][0]["message"]


def test_project_check_command_reports_missing_makefile_gate(tmp_path, capsys):
    destination = tmp_path / "billing_api"
    assert main(["new", str(destination), "--plugins", "auth"]) == 0
    capsys.readouterr()
    makefile = destination / "Makefile"
    makefile.write_text(
        read(makefile).replace("verify: config-check project-check test", "verify: test"),
        encoding="utf-8",
    )

    result = main(["project-check", str(destination), "--json"])

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert result == 1
    assert captured.err == ""
    assert report["valid"] is False
    assert report["issues"][0]["code"] == "makefile_missing_gate"
    assert report["issues"][0]["path"] == "Makefile"
    assert "verify: config-check project-check test" in report["issues"][0]["message"]


def test_project_check_command_reports_missing_prepare_env_script_gate(tmp_path, capsys):
    destination = tmp_path / "billing_api"
    assert main(["new", str(destination), "--plugins", "auth"]) == 0
    capsys.readouterr()
    script = destination / "scripts" / "prepare-env.sh"
    script.write_text(
        read(script).replace("secrets.token_urlsafe(32)", '"unsafe-placeholder"'),
        encoding="utf-8",
    )

    result = main(["project-check", str(destination), "--json"])

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert result == 1
    assert captured.err == ""
    assert report["valid"] is False
    assert report["issues"][0]["code"] == "prepare_env_script_missing_gate"
    assert report["issues"][0]["path"] == "scripts/prepare-env.sh"
    assert "secrets.token_urlsafe(32)" in report["issues"][0]["message"]


def test_project_check_command_reports_missing_agents_file_gate(tmp_path, capsys):
    destination = tmp_path / "billing_api"
    assert main(["new", str(destination), "--plugins", "auth"]) == 0
    capsys.readouterr()
    agents_path = destination / "AGENTS.md"
    agents_path.write_text(
        read(agents_path).replace(
            "Run `make verify` before handing off changes.",
            "Run whichever tests seem relevant.",
        ),
        encoding="utf-8",
    )

    result = main(["project-check", str(destination), "--json"])

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert result == 1
    assert captured.err == ""
    assert report["valid"] is False
    assert report["issues"][0]["code"] == "agents_file_missing_gate"
    assert report["issues"][0]["path"] == "AGENTS.md"
    assert "make verify" in report["issues"][0]["message"]


def test_project_check_command_reports_missing_compose_dependency(tmp_path, capsys):
    destination = tmp_path / "billing_api"
    assert main(["new", str(destination), "--plugins", "payment"]) == 0
    capsys.readouterr()
    compose = destination / "compose.yaml"
    compose.write_text(
        read(compose).replace("      MYSQL_HOST: mysql\n", ""),
        encoding="utf-8",
    )

    result = main(["project-check", str(destination), "--json"])

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert result == 1
    assert captured.err == ""
    assert report["valid"] is False
    assert report["issues"][0]["code"] == "compose_missing_mysql"
    assert report["issues"][0]["path"] == "compose.yaml"
    assert "MYSQL_HOST: mysql" in report["issues"][0]["message"]


def test_new_command_without_plugins_creates_minimal_project(tmp_path, capsys):
    destination = tmp_path / "billing_api"

    result = main(["new", str(destination)])

    captured = capsys.readouterr()
    assert result == 0
    assert "Next steps:" in captured.out
    assert "Release checks:" in captured.out
    assert captured.err == ""
    infra_toml = read(destination / "infra.toml")
    assert "[infra.plugins.ai]\nenabled = false" in infra_toml
    assert "[infra.plugins.auth]\nenabled = false" in infra_toml
    assert "[infra.plugins.payment]\nenabled = false" in infra_toml
    assert "Enabled plugins: none" in read(destination / "README.md")
    assert "infra.plugins.observability" not in read(destination / "app" / "main.py")
    assert not (destination / "app" / "worker.py").exists()


def test_new_command_can_use_profile_and_extra_plugins(tmp_path, capsys):
    destination = tmp_path / "billing_api"

    result = main(
        [
            "new",
            str(destination),
            "--profile",
            "api",
            "--plugins",
            "tasks",
        ]
    )

    captured = capsys.readouterr()
    infra_toml = read(destination / "infra.toml")
    assert result == 0
    assert "Profile: api" in captured.out
    assert "Plugins: auth, database, cache, http, observability, ratelimit, tasks" in captured.out
    for plugin in ("auth", "database", "cache", "http", "observability", "ratelimit", "tasks"):
        assert f"[infra.plugins.{plugin}]\nenabled = true" in infra_toml
    assert "[infra.plugins.payment]\nenabled = false" in infra_toml
    assert captured.err == ""


def test_new_command_rejects_unknown_profile(tmp_path, capsys):
    destination = tmp_path / "billing_api"

    result = main(["new", str(destination), "--profile", "enterprise"])

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert "unknown plugin profile: enterprise" in captured.err
    assert "available profiles:" in captured.err
    assert not destination.exists()


def test_new_command_rejects_unknown_plugins(tmp_path, capsys):
    destination = tmp_path / "billing_api"

    result = main(["new", str(destination), "--plugins", "ai,evil"])

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert "unknown plugin name: evil" in captured.err
    assert not destination.exists()


def test_new_command_can_scaffold_requested_external_plugin(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(
        "infra.plugins.discovery.entry_points",
        lambda group: [FakeEntryPoint(lambda: CLIEntryPointPlugin())],
    )
    destination = tmp_path / "billing_api"

    result = main(["new", str(destination), "--plugins", "external"])

    captured = capsys.readouterr()
    assert result == 0
    assert "Plugins: external" in captured.out
    assert captured.err == ""
    assert read(destination / "app" / "external.py") == (
        "def external_status() -> str:\n    return 'ready'\n"
    )
    assert '"fastapi-infra[http]"' in read(destination / "pyproject.toml")
    assert "[infra.plugins.external]\nenabled = true" in read(destination / "infra.toml")
    assert 'mode = "local"' in read(destination / "infra.toml")
    assert "EXTERNAL_API_KEY=" in read(destination / ".env.example")
    assert "Enabled plugins: external" in read(destination / "README.md")
    assert "This project was extended by an external plugin." in read(destination / "README.md")
    assert "EXPECTED_SERVICES = ['external']" in read(destination / "tests" / "test_health.py")


def test_new_command_force_overwrites_generated_files(tmp_path, capsys):
    destination = tmp_path / "billing_api"
    assert main(["new", str(destination)]) == 0
    (destination / "app" / "main.py").write_text("stale", encoding="utf-8")

    result = main(["new", str(destination), "--plugins", "auth", "--force"])

    captured = capsys.readouterr()
    assert result == 0
    assert captured.err == ""
    assert "stale" not in read(destination / "app" / "main.py")
    assert "[infra.plugins.auth]\nenabled = true" in read(destination / "infra.toml")


def test_plugins_init_command_creates_external_plugin_package(tmp_path, capsys):
    destination = tmp_path / "search_plugin"

    result = main(["plugins", "init", "vector_search", str(destination)])

    captured = capsys.readouterr()
    assert result == 0
    assert "Created plugin template" in captured.out
    assert (
        "fastapi-infra plugins check vector_search --settings infra.example.toml --lifecycle"
        in (captured.out)
    )
    assert captured.err == ""
    pyproject = read(destination / "pyproject.toml")
    plugin_module = read(destination / "src" / "fastapi_infra_vector_search_plugin" / "__init__.py")
    assert 'name = "fastapi-infra-vector-search-plugin"' in pyproject
    assert 'vector_search = "fastapi_infra_vector_search_plugin:VectorSearchPlugin"' in pyproject
    assert 'name="vector_search"' in plugin_module
    assert '"path": "app/vector_search.py"' in plugin_module
    assert "[infra.plugins.vector_search]" in read(destination / "infra.example.toml")
    assert "test_plugin_conforms_to_fastapi_infra_contract" in read(
        destination / "tests" / "test_plugin.py"
    )

    env = os.environ.copy()
    env["PYTHONPATH"] = str(destination / "src") + os.pathsep + env.get("PYTHONPATH", "")
    test_result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=destination,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert test_result.returncode == 0, test_result.stdout + test_result.stderr


def test_plugins_init_can_create_ai_provider_package(tmp_path, capsys):
    destination = tmp_path / "openrouter_provider"

    result = main(
        [
            "plugins",
            "init",
            "openrouter",
            str(destination),
            "--kind",
            "provider",
            "--provider-kind",
            "ai",
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert "Created provider template" in captured.out
    assert "fastapi-infra config-check --settings infra.example.toml" in captured.out
    assert "fastapi-infra new /tmp/openrouter-api --plugins ai" in captured.out
    assert captured.err == ""

    pyproject = read(destination / "pyproject.toml")
    provider_module = read(
        destination / "src" / "fastapi_infra_openrouter_ai_provider" / "__init__.py"
    )
    certification = read(
        destination / "src" / "fastapi_infra_openrouter_ai_provider" / "certification.py"
    )
    assert 'name = "fastapi-infra-openrouter-ai-provider"' in pyproject
    assert 'openrouter = "fastapi_infra_openrouter_ai_provider:create_provider"' in pyproject
    assert (
        'openrouter_ai = "fastapi_infra_openrouter_ai_provider.certification:provider_checks"'
        in pyproject
    )
    assert 'return "openrouter"' in provider_module
    assert 'message="OPENROUTER_AI_API_KEY is not configured"' in provider_module
    assert 'provider_kind="ai"' in certification
    assert 'provider_name="openrouter"' in certification
    assert 'default_provider = "openrouter"' in read(destination / "infra.example.toml")

    env = os.environ.copy()
    env["PYTHONPATH"] = str(destination / "src") + os.pathsep + env.get("PYTHONPATH", "")
    test_result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=destination,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert test_result.returncode == 0, test_result.stdout + test_result.stderr


def test_plugins_init_can_create_payment_provider_package(tmp_path, capsys):
    destination = tmp_path / "adyen_provider"

    result = main(
        [
            "plugins",
            "init",
            "adyen",
            str(destination),
            "--kind",
            "provider",
            "--provider-kind",
            "payment",
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert "Created provider template" in captured.out
    assert "fastapi-infra config-check --settings infra.example.toml" in captured.out
    assert "fastapi-infra new /tmp/adyen-api --plugins payment" in captured.out
    assert captured.err == ""

    pyproject = read(destination / "pyproject.toml")
    provider_module = read(
        destination / "src" / "fastapi_infra_adyen_payment_provider" / "__init__.py"
    )
    certification = read(
        destination / "src" / "fastapi_infra_adyen_payment_provider" / "certification.py"
    )
    assert 'name = "fastapi-infra-adyen-payment-provider"' in pyproject
    assert 'adyen = "fastapi_infra_adyen_payment_provider:create_provider"' in pyproject
    assert (
        'adyen_payment = "fastapi_infra_adyen_payment_provider.certification:provider_checks"'
        in pyproject
    )
    assert 'return "adyen"' in provider_module
    assert 'message="ADYEN_PAYMENT_API_KEY is not configured"' in provider_module
    assert 'provider_kind="payment"' in certification
    assert 'provider_name="adyen"' in certification
    assert 'default_provider = "adyen"' in read(destination / "infra.example.toml")

    env = os.environ.copy()
    env["PYTHONPATH"] = str(destination / "src") + os.pathsep + env.get("PYTHONPATH", "")
    test_result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=destination,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert test_result.returncode == 0, test_result.stdout + test_result.stderr


def test_plugins_init_can_create_speech_provider_package(tmp_path, capsys):
    destination = tmp_path / "deepgram_provider"

    result = main(
        [
            "plugins",
            "init",
            "deepgram",
            str(destination),
            "--kind",
            "provider",
            "--provider-kind",
            "speech",
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert "Created provider template" in captured.out
    assert "fastapi-infra config-check --settings infra.example.toml" in captured.out
    assert "fastapi-infra new /tmp/deepgram-api --plugins speech" in captured.out
    assert captured.err == ""

    pyproject = read(destination / "pyproject.toml")
    provider_module = read(
        destination / "src" / "fastapi_infra_deepgram_speech_provider" / "__init__.py"
    )
    certification = read(
        destination / "src" / "fastapi_infra_deepgram_speech_provider" / "certification.py"
    )
    assert 'name = "fastapi-infra-deepgram-speech-provider"' in pyproject
    assert 'deepgram = "fastapi_infra_deepgram_speech_provider:create_provider"' in pyproject
    assert (
        'deepgram_speech = "fastapi_infra_deepgram_speech_provider.certification:provider_checks"'
        in pyproject
    )
    assert 'return "deepgram"' in provider_module
    assert 'message="DEEPGRAM_SPEECH_API_KEY is not configured"' in provider_module
    assert 'provider_kind="speech"' in certification
    assert 'provider_name="deepgram"' in certification
    assert 'default_provider = "deepgram"' in read(destination / "infra.example.toml")

    env = os.environ.copy()
    env["PYTHONPATH"] = str(destination / "src") + os.pathsep + env.get("PYTHONPATH", "")
    test_result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=destination,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert test_result.returncode == 0, test_result.stdout + test_result.stderr


def test_plugins_init_can_create_storage_provider_package(tmp_path, capsys):
    destination = tmp_path / "r2_provider"

    result = main(
        [
            "plugins",
            "init",
            "r2",
            str(destination),
            "--kind",
            "provider",
            "--provider-kind",
            "storage",
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert "Created provider template" in captured.out
    assert "fastapi-infra config-check --settings infra.example.toml" in captured.out
    assert "fastapi-infra new /tmp/r2-api --plugins storage" in captured.out
    assert captured.err == ""

    pyproject = read(destination / "pyproject.toml")
    provider_module = read(
        destination / "src" / "fastapi_infra_r2_storage_provider" / "__init__.py"
    )
    certification = read(
        destination / "src" / "fastapi_infra_r2_storage_provider" / "certification.py"
    )
    assert 'name = "fastapi-infra-r2-storage-provider"' in pyproject
    assert 'r2 = "fastapi_infra_r2_storage_provider:create_provider"' in pyproject
    assert (
        'r2_storage = "fastapi_infra_r2_storage_provider.certification:provider_checks"'
        in pyproject
    )
    assert 'return "r2"' in provider_module
    assert 'message="R2_STORAGE_API_KEY is not configured"' in provider_module
    assert 'provider_kind="storage"' in certification
    assert 'provider_name="r2"' in certification
    assert 'default_provider = "r2"' in read(destination / "infra.example.toml")

    env = os.environ.copy()
    env["PYTHONPATH"] = str(destination / "src") + os.pathsep + env.get("PYTHONPATH", "")
    test_result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=destination,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert test_result.returncode == 0, test_result.stdout + test_result.stderr


def test_plugins_init_can_create_notifications_provider_package(tmp_path, capsys):
    destination = tmp_path / "twilio_provider"

    result = main(
        [
            "plugins",
            "init",
            "twilio",
            str(destination),
            "--kind",
            "provider",
            "--provider-kind",
            "notifications",
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert "Created provider template" in captured.out
    assert "fastapi-infra config-check --settings infra.example.toml" in captured.out
    assert "fastapi-infra new /tmp/twilio-api --plugins notifications" in captured.out
    assert captured.err == ""

    pyproject = read(destination / "pyproject.toml")
    provider_module = read(
        destination / "src" / "fastapi_infra_twilio_notifications_provider" / "__init__.py"
    )
    certification = read(
        destination / "src" / "fastapi_infra_twilio_notifications_provider" / "certification.py"
    )
    assert 'name = "fastapi-infra-twilio-notifications-provider"' in pyproject
    assert 'twilio = "fastapi_infra_twilio_notifications_provider:create_provider"' in pyproject
    assert (
        "twilio_notifications = "
        '"fastapi_infra_twilio_notifications_provider.certification:provider_checks"' in pyproject
    )
    assert '[project.entry-points."fastapi_infra.notification_providers"]' in pyproject
    assert 'return "twilio"' in provider_module
    assert 'message="TWILIO_NOTIFICATIONS_API_KEY is not configured"' in provider_module
    assert 'provider_kind="notifications"' in certification
    assert 'provider_name="twilio"' in certification
    assert 'default_provider = "twilio"' in read(destination / "infra.example.toml")

    env = os.environ.copy()
    env["PYTHONPATH"] = str(destination / "src") + os.pathsep + env.get("PYTHONPATH", "")
    test_result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=destination,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert test_result.returncode == 0, test_result.stdout + test_result.stderr


def test_plugins_init_can_create_webhook_provider_package(tmp_path, capsys):
    destination = tmp_path / "github_provider"

    result = main(
        [
            "plugins",
            "init",
            "github",
            str(destination),
            "--kind",
            "provider",
            "--provider-kind",
            "webhook",
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert "Created provider template" in captured.out
    assert "fastapi-infra config-check --settings infra.example.toml" in captured.out
    assert "fastapi-infra new /tmp/github-api --plugins webhooks" in captured.out
    assert captured.err == ""

    pyproject = read(destination / "pyproject.toml")
    provider_module = read(
        destination / "src" / "fastapi_infra_github_webhook_provider" / "__init__.py"
    )
    certification = read(
        destination / "src" / "fastapi_infra_github_webhook_provider" / "certification.py"
    )
    assert 'name = "fastapi-infra-github-webhook-provider"' in pyproject
    assert 'github = "fastapi_infra_github_webhook_provider:create_provider"' in pyproject
    assert (
        'github_webhook = "fastapi_infra_github_webhook_provider.certification:provider_checks"'
        in pyproject
    )
    assert '[project.entry-points."fastapi_infra.webhook_providers"]' in pyproject
    assert 'return "github"' in provider_module
    assert 'signature_header: str = "x-github-signature"' in provider_module
    assert 'provider_kind="webhook"' in certification
    assert 'provider_name="github"' in certification
    assert 'required_providers = ["github"]' in read(destination / "infra.example.toml")

    env = os.environ.copy()
    env["PYTHONPATH"] = str(destination / "src") + os.pathsep + env.get("PYTHONPATH", "")
    test_result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=destination,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert test_result.returncode == 0, test_result.stdout + test_result.stderr


def test_plugins_init_can_create_tasks_provider_package(tmp_path, capsys):
    destination = tmp_path / "sqs_provider"

    result = main(
        [
            "plugins",
            "init",
            "sqs",
            str(destination),
            "--kind",
            "provider",
            "--provider-kind",
            "tasks",
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert "Created provider template" in captured.out
    assert "fastapi-infra config-check --settings infra.example.toml" in captured.out
    assert "fastapi-infra new /tmp/sqs-api --plugins tasks" in captured.out
    assert captured.err == ""

    pyproject = read(destination / "pyproject.toml")
    provider_module = read(destination / "src" / "fastapi_infra_sqs_tasks_provider" / "__init__.py")
    certification = read(
        destination / "src" / "fastapi_infra_sqs_tasks_provider" / "certification.py"
    )
    assert 'name = "fastapi-infra-sqs-tasks-provider"' in pyproject
    assert 'sqs = "fastapi_infra_sqs_tasks_provider:create_provider"' in pyproject
    assert (
        'sqs_tasks = "fastapi_infra_sqs_tasks_provider.certification:provider_checks"' in pyproject
    )
    assert '[project.entry-points."fastapi_infra.task_queue_backends"]' in pyproject
    assert 'name = "sqs"' in provider_module
    assert 'queue_name: str = Field(default="sqs-default", min_length=1)' in provider_module
    assert 'provider_kind="tasks"' in certification
    assert 'provider_name="sqs"' in certification
    assert 'default_provider = "sqs"' in read(destination / "infra.example.toml")

    env = os.environ.copy()
    env["PYTHONPATH"] = str(destination / "src") + os.pathsep + env.get("PYTHONPATH", "")
    test_result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=destination,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert test_result.returncode == 0, test_result.stdout + test_result.stderr


def test_plugins_init_can_create_ratelimit_provider_package(tmp_path, capsys):
    destination = tmp_path / "upstash_provider"

    result = main(
        [
            "plugins",
            "init",
            "upstash",
            str(destination),
            "--kind",
            "provider",
            "--provider-kind",
            "ratelimit",
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert "Created provider template" in captured.out
    assert "fastapi-infra config-check --settings infra.example.toml" in captured.out
    assert "fastapi-infra new /tmp/upstash-api --plugins ratelimit" in captured.out
    assert captured.err == ""

    pyproject = read(destination / "pyproject.toml")
    provider_module = read(
        destination / "src" / "fastapi_infra_upstash_ratelimit_provider" / "__init__.py"
    )
    certification = read(
        destination / "src" / "fastapi_infra_upstash_ratelimit_provider" / "certification.py"
    )
    assert 'name = "fastapi-infra-upstash-ratelimit-provider"' in pyproject
    assert 'upstash = "fastapi_infra_upstash_ratelimit_provider:create_provider"' in pyproject
    assert (
        "upstash_ratelimit = "
        '"fastapi_infra_upstash_ratelimit_provider.certification:provider_checks"' in pyproject
    )
    assert '[project.entry-points."fastapi_infra.ratelimit_backends"]' in pyproject
    assert 'name = "upstash"' in provider_module
    assert 'key_prefix: str = Field(default="upstash:ratelimit", min_length=1)' in provider_module
    assert 'provider_kind="ratelimit"' in certification
    assert 'provider_name="upstash"' in certification
    assert 'default_provider = "upstash"' in read(destination / "infra.example.toml")

    env = os.environ.copy()
    env["PYTHONPATH"] = str(destination / "src") + os.pathsep + env.get("PYTHONPATH", "")
    test_result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=destination,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert test_result.returncode == 0, test_result.stdout + test_result.stderr


def test_plugins_init_rejects_invalid_plugin_name(tmp_path, capsys):
    result = main(["plugins", "init", "bad-name", str(tmp_path / "bad")])

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert "plugin name must start with a lowercase letter" in captured.err


def test_migrations_new_command_creates_sql_file(tmp_path, capsys):
    migrations = tmp_path / "migrations"

    result = main(["migrations", "new", str(migrations), "Create Users"])

    captured = capsys.readouterr()
    files = list(migrations.glob("*_create_users.sql"))
    assert result == 0
    assert len(files) == 1
    assert "Created migration" in captured.out
    assert captured.err == ""


def test_migrations_list_command_outputs_existing_migrations(tmp_path, capsys):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "20260512010203_create_users.sql").write_text(
        "CREATE TABLE users (id TEXT);\n",
        encoding="utf-8",
    )

    result = main(["migrations", "list", str(migrations)])

    captured = capsys.readouterr()
    assert result == 0
    assert "20260512010203 create_users" in captured.out
    assert captured.err == ""


def test_migrations_list_command_rejects_invalid_files(tmp_path, capsys):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "bad.sql").write_text("SELECT 1;\n", encoding="utf-8")

    result = main(["migrations", "list", str(migrations)])

    captured = capsys.readouterr()
    assert result == 1
    assert "invalid migration filename" in captured.err


def test_migrations_migrate_command_prints_applied_migrations(tmp_path, capsys, monkeypatch):
    from infra import cli

    class FakeMigration:
        version = "20260512010203"
        name = "create_users"

    async def fake_apply_migrations(migrations_path: Path, settings_path: Path):
        assert migrations_path == tmp_path / "migrations"
        assert settings_path == tmp_path / "infra.toml"
        return [FakeMigration()]

    monkeypatch.setattr(cli, "_apply_migrations", fake_apply_migrations)

    result = main(
        [
            "migrations",
            "migrate",
            str(tmp_path / "migrations"),
            "--settings",
            str(tmp_path / "infra.toml"),
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert "Applied 20260512010203 create_users" in captured.out
    assert captured.err == ""


def test_migrations_migrate_command_reports_errors(tmp_path, capsys, monkeypatch):
    from infra import cli

    async def fake_apply_migrations(migrations_path: Path, settings_path: Path):
        raise RuntimeError("database plugin is not enabled")

    monkeypatch.setattr(cli, "_apply_migrations", fake_apply_migrations)

    result = main(
        [
            "migrations",
            "migrate",
            str(tmp_path / "migrations"),
            "--settings",
            str(tmp_path / "infra.toml"),
        ]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert "database plugin is not enabled" in captured.err


def test_certify_providers_command_delegates_to_certification_runner(capsys, monkeypatch):
    from infra import cli

    calls = []

    def fake_run(test_path, checks, *, json_output, environ=None):
        calls.append((test_path, [check.name for check in checks], json_output, environ))
        return 0

    monkeypatch.setattr(cli, "run_pytest_certification", fake_run)

    result = main(
        [
            "certify-providers",
            "--provider",
            "stripe",
            "--test-path",
            "tests/custom.py",
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert calls == [("tests/custom.py", ["mysql", "stripe"], False, None)]
    assert captured.err == ""


def test_certify_providers_command_can_request_json_output(capsys, monkeypatch):
    from infra import cli

    calls = []

    def fake_run(test_path, checks, *, json_output, environ=None):
        calls.append((test_path, [check.name for check in checks], json_output, environ))
        return 0

    monkeypatch.setattr(cli, "run_pytest_certification", fake_run)

    result = main(["certify-providers", "--provider", "stripe", "--json"])

    captured = capsys.readouterr()
    assert result == 0
    assert calls == [
        (None, ["mysql", "stripe"], True, None),
    ]
    assert captured.err == ""


def test_certify_providers_command_can_run_preflight(capsys, monkeypatch):
    from infra import cli

    calls = []

    def fake_preflight(checks, *, json_output, environ=None):
        calls.append(([check.name for check in checks], json_output, environ))
        return 1

    monkeypatch.setattr(cli, "run_provider_preflight", fake_preflight)

    result = main(["certify-providers", "--provider", "stripe", "--preflight", "--json"])

    captured = capsys.readouterr()
    assert result == 1
    assert calls == [(["mysql", "stripe"], True, None)]
    assert captured.err == ""


def test_certify_providers_command_can_select_checks_from_settings(
    tmp_path,
    capsys,
    monkeypatch,
):
    from infra import cli

    settings_path = tmp_path / "infra.toml"
    settings_path.write_text(
        """
[infra.plugins.payment]
enabled = true

[infra.plugins.payment.config]
default_provider = "stripe"
health_probe = true
store_service = "database"

[infra.plugins.payment.config.providers.stripe]
api_key = "sk-stripe"
webhook_secret = "whsec_test"
""",
        encoding="utf-8",
    )
    calls = []

    def fake_preflight(checks, *, json_output, environ=None):
        calls.append(([check.name for check in checks], json_output, environ))
        return 0

    monkeypatch.setattr(cli, "run_provider_preflight", fake_preflight)

    result = main(
        [
            "certify-providers",
            "--settings",
            str(settings_path),
            "--preflight",
            "--json",
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert calls == [(["mysql", "stripe"], True, None)]
    assert captured.err == ""


def test_certify_providers_command_settings_list_is_limited_to_required_checks(
    tmp_path,
    capsys,
):
    settings_path = tmp_path / "infra.toml"
    settings_path.write_text(
        """
[infra.plugins.ai]
enabled = true

[infra.plugins.ai.config]
default_provider = "openai"
health_probe = true

[infra.plugins.ai.config.providers.openai]
api_key = "sk-openai"
""",
        encoding="utf-8",
    )

    result = main(
        [
            "certify-providers",
            "--settings",
            str(settings_path),
            "--list",
            "--requirements",
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert "openai-ai:" in captured.out
    assert "OPENAI_LIVE_CHAT_MODEL" in captured.out
    assert "stripe:" not in captured.out
    assert captured.err == ""


def test_certify_providers_command_uses_separate_settings_and_provider_env_files(
    tmp_path,
    capsys,
    monkeypatch,
):
    from infra import cli

    settings_path = tmp_path / "infra.toml"
    settings_path.write_text(
        """
[infra.plugins.ai]
enabled = true

[infra.plugins.ai.config]
default_provider = "openai"
health_probe = true

[infra.plugins.ai.config.providers.openai]
api_key = { "$env" = "OPENAI_API_KEY" }
""",
        encoding="utf-8",
    )
    settings_env_file = tmp_path / ".env"
    settings_env_file.write_text("OPENAI_API_KEY=runtime-key\n", encoding="utf-8")
    provider_env_file = tmp_path / "provider.env"
    provider_env_file.write_text(
        "\n".join(
            [
                "OPENAI_API_KEY=live-key",
                "OPENAI_LIVE_CHAT_MODEL=gpt-test",
                "OPENAI_LIVE_EMBEDDING_MODEL=text-embedding-test",
            ]
        ),
        encoding="utf-8",
    )
    calls = []

    def fake_preflight(checks, *, json_output, environ=None):
        calls.append(([check.name for check in checks], json_output, environ))
        return 0

    monkeypatch.setattr(cli, "run_provider_preflight", fake_preflight)

    result = main(
        [
            "certify-providers",
            "--settings",
            str(settings_path),
            "--settings-env-file",
            str(settings_env_file),
            "--preflight",
            "--env-file",
            str(provider_env_file),
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert calls[0][0] == ["openai-ai"]
    assert calls[0][1] is False
    assert calls[0][2]["OPENAI_API_KEY"] == "live-key"
    assert calls[0][2]["OPENAI_LIVE_CHAT_MODEL"] == "gpt-test"
    assert captured.err == ""


def test_certify_providers_command_can_load_env_file(tmp_path, capsys, monkeypatch):
    from infra import cli

    calls = []
    env_file = tmp_path / "provider.env"
    env_file.write_text(
        "STRIPE_API_KEY=sk-file\nSTRIPE_WEBHOOK_SECRET=whsec-file\n",
        encoding="utf-8",
    )

    def fake_preflight(checks, *, json_output, environ=None):
        calls.append(([check.name for check in checks], json_output, environ))
        return 0

    monkeypatch.setattr(cli, "run_provider_preflight", fake_preflight)

    result = main(
        [
            "certify-providers",
            "--provider",
            "stripe",
            "--preflight",
            "--env-file",
            str(env_file),
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert calls[0][0] == ["mysql", "stripe"]
    assert calls[0][1] is False
    assert calls[0][2]["STRIPE_API_KEY"] == "sk-file"
    assert calls[0][2]["STRIPE_WEBHOOK_SECRET"] == "whsec-file"
    assert captured.err == ""


def test_certify_providers_command_reports_missing_env_file(tmp_path, capsys):
    result = main(
        [
            "certify-providers",
            "--provider",
            "stripe",
            "--preflight",
            "--env-file",
            str(tmp_path / "missing.env"),
        ]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert "env file could not be read" in captured.err


def test_certify_providers_list_does_not_require_env_file(tmp_path, capsys):
    result = main(
        [
            "certify-providers",
            "--provider",
            "stripe",
            "--list",
            "--env-file",
            str(tmp_path / "missing.env"),
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert "stripe:" in captured.out
    assert captured.err == ""


def test_certify_providers_command_rejects_unknown_provider(capsys):
    result = main(["certify-providers", "--provider", "unknown"])

    captured = capsys.readouterr()
    assert result == 2
    assert captured.out == ""
    assert "unknown provider check: unknown" in captured.err


def test_certify_providers_command_lists_provider_checks(capsys):
    result = main(["certify-providers", "--list"])

    captured = capsys.readouterr()
    assert result == 0
    assert "stripe:" in captured.out
    assert "test_live_stripe_webhook_signature_entrypoint" in captured.out
    assert "required env:" not in captured.out
    assert captured.err == ""


def test_certify_providers_command_lists_provider_requirements(capsys):
    result = main(["certify-providers", "--list", "--requirements"])

    captured = capsys.readouterr()
    assert result == 0
    assert "stripe:" in captured.out
    assert "required env:" in captured.out
    assert "STRIPE_API_KEY" in captured.out
    assert "optional env:" in captured.out
    assert "STRIPE_LIVE_TIMEOUT" in captured.out
    assert captured.err == ""


def test_certify_providers_list_honors_selected_provider(capsys):
    result = main(["certify-providers", "--provider", "stripe", "--list", "--requirements"])

    captured = capsys.readouterr()
    assert result == 0
    assert "mysql:" in captured.out
    assert "stripe:" in captured.out
    assert "STRIPE_API_KEY" in captured.out
    assert "s3:" not in captured.out
    assert "OPENAI_API_KEY" not in captured.out
    assert captured.err == ""


def test_certify_providers_command_prints_env_template_for_selected_provider(capsys):
    result = main(["certify-providers", "--provider", "stripe", "--env-template"])

    captured = capsys.readouterr()
    assert result == 0
    assert "STRIPE_API_KEY=" in captured.out
    assert "STRIPE_WEBHOOK_SECRET=" in captured.out
    assert "MYSQL_LIVE_HOST=" in captured.out
    assert "# STRIPE_API_BASE=" in captured.out
    assert "S3_LIVE_BUCKET" not in captured.out
    assert captured.err == ""


def test_certify_providers_env_template_can_select_from_settings_without_env(
    tmp_path,
    capsys,
    monkeypatch,
):
    monkeypatch.delenv("STRIPE_API_KEY", raising=False)
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    settings_path = tmp_path / "infra.toml"
    settings_path.write_text(
        """
[infra.plugins.payment]
enabled = true

[infra.plugins.payment.config]
default_provider = "stripe"
health_probe = true
store_service = "database"

[infra.plugins.payment.config.providers.stripe]
api_key = { "$env" = "STRIPE_API_KEY" }
webhook_secret = { "$env" = "STRIPE_WEBHOOK_SECRET" }
""",
        encoding="utf-8",
    )

    result = main(
        [
            "certify-providers",
            "--settings",
            str(settings_path),
            "--env-template",
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert "MYSQL_LIVE_HOST=" in captured.out
    assert "STRIPE_API_KEY=" in captured.out
    assert "STRIPE_WEBHOOK_SECRET=" in captured.out
    assert "S3_LIVE_BUCKET" not in captured.out
    assert captured.err == ""


def test_plugins_command_prints_human_readable_manifest(capsys):
    result = main(["plugins"])

    captured = capsys.readouterr()
    assert result == 0
    assert "ai disabled provides=ai service_keys=ai:infra.plugins.AI_SERVICE" in captured.out
    assert (
        "tasks disabled provides=tasks service_name_config=service "
        "service_keys=tasks:infra.plugins.TASKS_SERVICE" in captured.out
    )
    assert captured.err == ""


def test_plugins_command_prints_json_manifest(capsys):
    result = main(["plugins", "--json"])

    captured = capsys.readouterr()
    manifest = json.loads(captured.out)
    assert result == 0
    assert manifest["ai"]["provides"] == ["ai"]
    assert manifest["ai"]["configured_services"] == ["ai"]
    assert manifest["ai"]["service_keys"] == {"ai": "infra.plugins.AI_SERVICE"}
    assert manifest["ai"]["recommended_extras"] == ["ai"]
    assert "OPENAI_API_KEY" in manifest["ai"]["env_vars"]
    assert manifest["ai"]["production_config_example"]["default_provider"] == "openai"
    assert manifest["tasks"]["service_name_config"] == "service"
    assert manifest["tasks"]["configured_services"] == ["tasks"]
    assert manifest["tasks"]["service_keys"] == {"tasks": "infra.plugins.TASKS_SERVICE"}
    assert manifest["tasks"]["service_references"]["providers.redis.database_service"] == {
        "default_service": "database",
        "description": "Database service that provides get_redis_client().",
        "optional": False,
        "required_unless_config": {},
        "required_when": "default_provider == 'redis' and no Redis client is injected",
        "required_when_config": {"default_provider": "redis"},
    }
    assert (
        manifest["payment"]["production_config_example"]["providers"]["stripe"]["webhook_secret"]
        == "${STRIPE_WEBHOOK_SECRET}"
    )
    assert "config_schema" in manifest["payment"]
    assert captured.err == ""


def test_plugins_check_command_reports_valid_builtin_plugin(capsys):
    result = main(["plugins", "check", "ai", "--json"])

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert result == 0
    assert report["valid"] is True
    assert report["summary"] == {"errors": 0, "warnings": 0}
    assert report["plugins"][0]["name"] == "ai"
    assert report["plugins"][0]["valid"] is True
    assert report["plugins"][0]["issues"] == []
    assert captured.err == ""


def test_plugins_check_command_can_lifecycle_check_external_plugin(capsys, monkeypatch):
    monkeypatch.setattr(
        "infra.plugins.discovery.entry_points",
        lambda group: [FakeEntryPoint(lambda: CLIEntryPointPlugin())],
    )

    result = main(["plugins", "check", "external", "--lifecycle"])

    captured = capsys.readouterr()
    assert result == 0
    assert "plugins check: valid" in captured.out
    assert "- external: valid" in captured.out
    assert captured.err == ""


def test_plugins_check_command_reports_invalid_external_plugin(capsys, monkeypatch):
    monkeypatch.setattr(
        "infra.plugins.discovery.entry_points",
        lambda group: [FakeEntryPoint(lambda: InvalidCLIEntryPointPlugin())],
    )

    result = main(["plugins", "check", "external", "--json"])

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert result == 1
    assert report["valid"] is False
    assert report["plugins"][0]["name"] == "external"
    assert report["plugins"][0]["issues"][0]["code"] == "undeclared_service_key"
    assert "missing" in report["plugins"][0]["issues"][0]["message"]
    assert captured.err == ""


def test_plugins_check_command_reports_unknown_plugin(capsys):
    result = main(["plugins", "check", "missing"])

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert "unknown plugin name: missing" in captured.err


def test_profiles_command_prints_human_readable_profiles(capsys):
    result = main(["profiles"])

    captured = capsys.readouterr()
    assert result == 0
    assert "minimal: none - Core FastAPI project" in captured.out
    assert "api: auth, database, cache, http, observability, ratelimit" in captured.out
    assert "saas: auth, database, cache, http, observability, payment" in captured.out
    assert "full:" in captured.out
    assert captured.err == ""


def test_profiles_command_prints_json_profiles(capsys):
    result = main(["profiles", "--json"])

    captured = capsys.readouterr()
    profiles = json.loads(captured.out)
    assert result == 0
    assert profiles["minimal"]["plugins"] == []
    assert profiles["api"]["plugins"] == [
        "auth",
        "database",
        "cache",
        "http",
        "observability",
        "ratelimit",
    ]
    assert "SaaS foundation" in profiles["saas"]["description"]
    assert "ai" in profiles["full"]["plugins"]
    assert "payment" in profiles["full"]["plugins"]
    assert captured.err == ""


def test_plugins_command_can_load_settings_file(tmp_path, capsys):
    settings_path = tmp_path / "infra.toml"
    settings_path.write_text(
        """
[infra.plugins.ai]
enabled = true

[infra.plugins.tasks]
enabled = false

[infra.plugins.tasks.config]
service = "jobs"
""",
        encoding="utf-8",
    )

    result = main(["plugins", "--settings", str(settings_path), "--json"])

    captured = capsys.readouterr()
    manifest = json.loads(captured.out)
    assert result == 0
    assert manifest["ai"]["configured_enabled"] is True
    assert manifest["tasks"]["configured_enabled"] is False
    assert manifest["tasks"]["configured_services"] == ["tasks", "jobs"]
    assert captured.err == ""


def test_plugins_command_reports_missing_settings_file(tmp_path, capsys):
    missing = tmp_path / "missing.toml"

    result = main(["plugins", "--settings", str(missing), "--json"])

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert f"settings file not found: {missing}" in captured.err


def test_config_check_command_reports_invalid_plugin_config(tmp_path, capsys):
    settings_path = tmp_path / "infra.toml"
    settings_path.write_text(
        """
[infra.plugins.ai]
enabled = true

[infra.plugins.ai.config.providers.openai]
api_kee = "typo"
""",
        encoding="utf-8",
    )

    result = main(["config-check", "--settings", str(settings_path), "--json"])

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert result == 1
    assert report["valid"] is False
    assert report["issues"][0]["plugin"] == "ai"
    assert report["issues"][0]["code"] == "invalid_config"
    assert "api_kee" in report["issues"][0]["message"]
    assert captured.err == ""


def test_config_check_command_reports_missing_service_reference(tmp_path, capsys):
    settings_path = tmp_path / "infra.toml"
    settings_path.write_text(
        """
[infra.plugins.payment]
enabled = true

[infra.plugins.payment.config]
store_service = "database"
""",
        encoding="utf-8",
    )

    result = main(["config-check", "--settings", str(settings_path), "--json"])

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert result == 1
    assert report["valid"] is False
    assert report["issues"][0]["plugin"] == "payment"
    assert report["issues"][0]["code"] == "missing_service_reference"
    assert report["issues"][0]["details"]["field"] == "store_service"
    assert captured.err == ""


def test_config_check_command_reports_valid_settings(tmp_path, capsys):
    settings_path = tmp_path / "infra.toml"
    settings_path.write_text(
        """
[infra.plugins.ai]
enabled = true

[infra.plugins.ai.config.providers.openai]
api_key = "sk-test"
""",
        encoding="utf-8",
    )

    result = main(["config-check", "--settings", str(settings_path)])

    captured = capsys.readouterr()
    assert result == 0
    assert "config-check: valid" in captured.out
    assert captured.err == ""


def test_config_check_command_can_load_env_file(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("JWT_SECRET", raising=False)
    settings_path = tmp_path / "infra.toml"
    settings_path.write_text(
        """
[infra.plugins.auth]
enabled = true

[infra.plugins.auth.config]
jwt_secret = { "$env" = "JWT_SECRET" }
""",
        encoding="utf-8",
    )
    env_file = tmp_path / "infra.env"
    env_file.write_text("JWT_SECRET=secret-from-file\n", encoding="utf-8")

    result = main(
        [
            "config-check",
            "--settings",
            str(settings_path),
            "--env-file",
            str(env_file),
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert "config-check: valid" in captured.out
    assert captured.err == ""
    assert "JWT_SECRET" not in os.environ


def test_config_check_command_reports_missing_env_file(tmp_path, capsys):
    settings_path = tmp_path / "infra.toml"
    settings_path.write_text("[infra.plugins]\n", encoding="utf-8")

    result = main(
        [
            "config-check",
            "--settings",
            str(settings_path),
            "--env-file",
            str(tmp_path / "missing.env"),
        ]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert "env file could not be read" in captured.err


def test_release_check_command_reports_blocking_configuration(tmp_path, capsys):
    settings_path = tmp_path / "infra.toml"
    settings_path.write_text(
        """
[infra.plugins.ai]
enabled = true

[infra.plugins.ai.config]
default_provider = "mock"
""",
        encoding="utf-8",
    )

    result = main(["release-check", "--settings", str(settings_path), "--json"])

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert result == 1
    assert report["ready"] is False
    assert report["issues"][0]["plugin"] == "ai"
    assert report["issues"][0]["code"] == "mock_provider"
    assert captured.err == ""


def test_release_check_command_requires_existing_settings_file(tmp_path, capsys):
    missing = tmp_path / "missing.toml"

    result = main(["release-check", "--settings", str(missing)])

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert f"settings file not found: {missing}" in captured.err


def test_release_check_command_blocks_failed_provider_certification_report(tmp_path, capsys):
    settings_path = tmp_path / "infra.toml"
    settings_path.write_text("[infra.plugins]\n", encoding="utf-8")
    certification_path = tmp_path / "provider-certification.json"
    certification_path.write_text(
        json.dumps({"certified": False, "summary": {"total": 1, "failed": 1}}),
        encoding="utf-8",
    )

    result = main(
        [
            "release-check",
            "--settings",
            str(settings_path),
            "--provider-certification-report",
            str(certification_path),
            "--json",
        ]
    )

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert result == 1
    assert report["ready"] is False
    assert report["issues"][0]["plugin"] == "providers"
    assert report["issues"][0]["code"] == "certification_not_passed"
    assert captured.err == ""


def test_release_check_command_requires_provider_certification_report_by_default(tmp_path, capsys):
    settings_path = tmp_path / "infra.toml"
    settings_path.write_text(
        """
[infra.plugins.ai]
enabled = true

[infra.plugins.ai.config]
default_provider = "openai"
health_probe = true

[infra.plugins.ai.config.providers.openai]
api_key = "sk-test"
""",
        encoding="utf-8",
    )

    result = main(
        [
            "release-check",
            "--settings",
            str(settings_path),
            "--json",
        ]
    )

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert result == 1
    assert report["ready"] is False
    assert report["issues"] == [
        {
            "severity": "error",
            "plugin": "providers",
            "code": "certification_report_required",
            "message": "provider certification report is required for: openai-ai",
        }
    ]
    assert captured.err == ""


def test_release_check_command_can_run_static_only(tmp_path, capsys):
    settings_path = tmp_path / "infra.toml"
    settings_path.write_text(
        """
[infra.plugins.ai]
enabled = true

[infra.plugins.ai.config]
default_provider = "openai"
health_probe = true

[infra.plugins.ai.config.providers.openai]
api_key = "sk-test"
""",
        encoding="utf-8",
    )

    result = main(
        [
            "release-check",
            "--settings",
            str(settings_path),
            "--static-only",
            "--json",
        ]
    )

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert result == 0
    assert report["ready"] is True
    assert report["issues"] == []
    assert captured.err == ""


def test_release_check_command_can_validate_migrations(tmp_path, capsys):
    settings_path = tmp_path / "infra.toml"
    settings_path.write_text(
        """
[infra.plugins.payment]
enabled = true

[infra.plugins.payment.config]
default_provider = "mock"
""",
        encoding="utf-8",
    )

    result = main(
        [
            "release-check",
            "--settings",
            str(settings_path),
            "--migrations",
            str(tmp_path / "migrations"),
            "--static-only",
            "--json",
        ]
    )

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert result == 1
    assert {
        "severity": "error",
        "plugin": "payment",
        "code": "migration_missing",
        "message": (
            "required plugin migration is missing: " "00000000001000_infra_payment_store.sql"
        ),
    } in report["issues"]
    assert captured.err == ""


def test_release_check_command_can_load_env_file(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    settings_path = tmp_path / "infra.toml"
    settings_path.write_text(
        """
[infra.plugins.ai]
enabled = true

[infra.plugins.ai.config]
default_provider = "openai"
health_probe = true

[infra.plugins.ai.config.providers.openai]
api_key = { "$env" = "OPENAI_API_KEY" }
""",
        encoding="utf-8",
    )
    env_file = tmp_path / "provider.env"
    env_file.write_text("OPENAI_API_KEY=sk-file\n", encoding="utf-8")

    result = main(
        [
            "release-check",
            "--settings",
            str(settings_path),
            "--env-file",
            str(env_file),
            "--static-only",
            "--json",
        ]
    )

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert result == 0
    assert report["ready"] is True
    assert report["issues"] == []
    assert captured.err == ""
    assert "OPENAI_API_KEY" not in os.environ


def test_release_check_command_reports_missing_env_file(tmp_path, capsys):
    settings_path = tmp_path / "infra.toml"
    settings_path.write_text("[infra.plugins]\n", encoding="utf-8")

    result = main(
        [
            "release-check",
            "--settings",
            str(settings_path),
            "--env-file",
            str(tmp_path / "missing.env"),
        ]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert "env file could not be read" in captured.err


def test_release_check_command_uses_available_plugin_release_checks(
    tmp_path,
    capsys,
    monkeypatch,
):
    from infra import cli

    class ExternalReleaseCheckPlugin:
        metadata = PluginMetadata(name="external", version="1.0.0")
        config_model = None

        def release_check(
            self,
            settings: InfraSettings,
            config: object,
        ) -> list[ReleaseCheckIssue]:
            return [
                ReleaseCheckIssue(
                    plugin="external",
                    code="blocked",
                    message="external plugin blocked release",
                )
            ]

    settings_path = tmp_path / "infra.toml"
    settings_path.write_text(
        """
[infra.plugins.external]
enabled = true
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cli,
        "get_available_plugins",
        lambda settings=None: [ExternalReleaseCheckPlugin()],
    )

    result = main(
        [
            "release-check",
            "--settings",
            str(settings_path),
            "--static-only",
            "--json",
        ]
    )

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert result == 1
    assert report["issues"] == [
        {
            "severity": "error",
            "plugin": "external",
            "code": "blocked",
            "message": "external plugin blocked release",
        }
    ]
    assert captured.err == ""


def test_release_check_command_evaluates_release_checks_once(
    tmp_path,
    capsys,
    monkeypatch,
):
    from infra import cli

    calls = 0

    class ExternalReleaseCheckPlugin:
        metadata = PluginMetadata(name="external", version="1.0.0")
        config_model = None

        def release_check(
            self,
            settings: InfraSettings,
            config: object,
        ) -> list[ReleaseCheckIssue]:
            nonlocal calls
            calls += 1
            return []

    settings_path = tmp_path / "infra.toml"
    settings_path.write_text(
        """
[infra.plugins.external]
enabled = true
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cli,
        "get_available_plugins",
        lambda settings=None: [ExternalReleaseCheckPlugin()],
    )

    result = main(
        [
            "release-check",
            "--settings",
            str(settings_path),
            "--static-only",
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert calls == 1
    assert "release-check: ready" in captured.out
    assert captured.err == ""
