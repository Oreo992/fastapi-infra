from pathlib import Path


def test_infra_package_has_no_original_business_imports():
    root = Path("infra")
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "from app." in text or "import app." in text:
            offenders.append(str(path))
        if "PersonalityTest" in text or "MusicSync" in text:
            offenders.append(str(path))

    assert offenders == []
