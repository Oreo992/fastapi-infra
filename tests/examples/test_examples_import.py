import importlib.util
from pathlib import Path


def import_file(path: str):
    file_path = Path(path)
    module_name = "_".join(file_path.with_suffix("").parts)
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_minimal_example_imports():
    module = import_file("examples/minimal/app.py")
    assert hasattr(module, "app")


def test_ai_example_imports():
    module = import_file("examples/ai_app/app.py")
    assert hasattr(module, "app")


def test_full_stack_example_imports():
    module = import_file("examples/full_stack/app.py")
    assert hasattr(module, "app")
