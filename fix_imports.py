#!/usr/bin/env python3
"""
批量修改导入路径

将母包的导入路径替换为新包的导入路径
"""
import re
from pathlib import Path

# 导入路径映射表
REPLACEMENTS = {
    "from app.core.logging_pkg.logger import": "from infra.logging import",
    "from app.core.common.exceptions import": "from infra.exceptions import",
    "from app.core.infrastructure.http.resilience import": "from infra.http.resilience import",
    "from app.core.infrastructure.http.http_client import": "from infra.http.client import",
    "from app.core.infrastructure.db.database import": "from infra.database.manager import",
    "from app.core.infrastructure.db.base_repository import": "from infra.database.repository import",
    "from app.core.configuration.config import": "from infra.config import",
    "from app.core.common.registry import": "from infra.registry import",
    "from app.core.common.service_factory import": "from infra.registry import",
    "from app.utils.timezone_helper import": "from infra.utils.timezone import",
    "from app.interfaces.": "# from app.interfaces.",  # 注释掉业务接口导入
}

def fix_file(filepath: Path):
    """修复单个文件的导入"""
    try:
        content = filepath.read_text(encoding="utf-8")
        modified = content
        
        changes = []
        for old, new in REPLACEMENTS.items():
            if old in modified:
                modified = modified.replace(old, new)
                changes.append(f"{old} -> {new}")
        
        if modified != content:
            filepath.write_text(modified, encoding="utf-8")
            print(f"[OK] 已修复: {filepath}")
            for change in changes:
                print(f"  - {change}")
        else:
            print(f"[SKIP] {filepath}")
    except Exception as e:
        print(f"[ERROR] {filepath}: {e}")

def main():
    """主函数"""
    infra_dir = Path("infra")
    
    if not infra_dir.exists():
        print("错误: infra/ 目录不存在")
        print("请在 fastapi-infra 根目录运行此脚本")
        return
    
    print("开始批量修复导入路径...")
    print("=" * 60)
    
    py_files = list(infra_dir.rglob("*.py"))
    print(f"找到 {len(py_files)} 个 Python 文件\n")
    
    for py_file in py_files:
        if py_file.name == "__init__.py":
            continue  # 跳过 __init__.py
        fix_file(py_file)
        print()
    
    print("=" * 60)
    print("✓ 批量修复完成!")

if __name__ == "__main__":
    main()
