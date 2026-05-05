from pathlib import Path


def test_infra_package_has_no_original_business_imports():
    root = Path("infra")
    offenders: list[str] = []
    denied_terms = [
        "from app.",
        "import app.",
        "Agent",
        "Tool",
        "LLM",
        "PersonalityTest",
        "MusicSync",
        "塔罗",
        "音乐",
        "占星",
        "业务逻辑",
        "业务异常",
        "用户信息",
        "工具",
    ]
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if any(term in text for term in denied_terms):
            offenders.append(str(path))

    assert offenders == []
