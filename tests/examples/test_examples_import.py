import importlib.util
from pathlib import Path
from types import ModuleType


def import_file(path: str) -> ModuleType:
    file_path = Path(path)
    module_name = "_".join(file_path.with_suffix("").parts)
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_minimal_example_imports():
    module = import_file("examples/minimal/app.py")
    assert hasattr(module, "app")


def test_ai_example_imports():
    module = import_file("examples/ai_app/app.py")
    assert hasattr(module, "app")
    assert "AI_SERVICE" in Path("examples/ai_app/app.py").read_text(encoding="utf-8")


def test_full_stack_example_imports():
    module = import_file("examples/full_stack/app.py")
    assert hasattr(module, "app")
    content = Path("examples/full_stack/app.py").read_text(encoding="utf-8")
    assert "PAYMENT_SERVICE" in content
    assert 'infra.get("payment")' not in content


def test_full_stack_example_starts_and_accepts_dev_key():
    from fastapi.testclient import TestClient

    module = import_file("examples/full_stack/app.py")

    with TestClient(module.app) as client:
        response = client.post(
            "/checkout",
            headers={"X-API-Key": "dev-key"},
            json={"amount": 1200, "currency": "usd", "reference": "order-1"},
        )

    assert response.status_code == 200
    assert response.json()["reference"] == "order-1"


def test_external_search_plugin_example_imports_and_declares_entry_point():
    import tomllib

    package_root = Path("examples/search_plugin")
    pyproject = tomllib.loads(package_root.joinpath("pyproject.toml").read_text(encoding="utf-8"))
    module = import_file("examples/search_plugin/src/fastapi_infra_search_plugin/__init__.py")

    assert pyproject["project"]["entry-points"]["fastapi_infra.plugins"]["search"] == (
        "fastapi_infra_search_plugin:SearchPlugin"
    )
    assert module.SearchPlugin.metadata.name == "search"
    assert module.SearchPlugin.metadata.provides == ["search"]
    assert module.SearchPlugin.config_model is module.SearchConfig
    assert module.SearchPlugin.manifest_hints["scaffold_files"][0]["path"] == "app/search.py"
    assert module.SearchService(module.SearchConfig()).config.endpoint == "memory://search"
