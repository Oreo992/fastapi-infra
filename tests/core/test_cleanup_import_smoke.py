import importlib
from pathlib import Path


def test_task4_cleanup_keeps_internal_modules_importable():
    root = Path("infra")
    modules = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        module_path = path.with_suffix("")
        if module_path.name == "__init__":
            module_path = module_path.parent
        modules.append(".".join(module_path.parts))

    for module_name in modules:
        importlib.import_module(module_name)
