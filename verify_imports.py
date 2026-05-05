#!/usr/bin/env python3
"""
快速验证脚本 - 检查所有模块是否可正常导入

运行: python verify_imports.py
"""

import sys


def test_import(module_name, items=None):
    """测试导入模块或模块中的项"""
    try:
        if items:
            for item in items:
                exec(f"from {module_name} import {item}")
            print(f"[OK] {module_name} ({', '.join(items)})")
        else:
            exec(f"import {module_name}")
            print(f"[OK] {module_name}")
        return True
    except Exception as e:
        print(f"[FAIL] {module_name}: {e}")
        return False


def main():
    """主函数"""
    print("=" * 60)
    print("FastAPI-Infra 导入验证")
    print("=" * 60)
    print()
    
    tests = [
        # 核心模块
        ("infra", None),
        ("infra.http", ["HttpClient", "HttpResponse"]),
        ("infra.database", ["DatabaseManager", "BaseRepository"]),
        ("infra.cache", ["CacheService"]),
        ("infra.logging", ["get_logger", "LoggerManager"]),
        ("infra.registry", ["ServiceRegistry", "ServiceContainer"]),
        ("infra.config", ["BaseSettings"]),
        ("infra.exceptions", ["AppException", "RepositoryError"]),
        ("infra.utils", ["get_timestamp"]),
        
        # 插件
        ("infra.plugins", None),
        
        # 具体组件
        ("infra.http.client", ["HttpClient"]),
        ("infra.http.resilience", ["with_resilience", "PresetConfigs"]),
        ("infra.database.manager", ["DatabaseManager"]),
        ("infra.database.repository", ["BaseRepository"]),
    ]
    
    passed = 0
    failed = 0
    
    for module, items in tests:
        if test_import(module, items):
            passed += 1
        else:
            failed += 1
    
    print()
    print("=" * 60)
    print(f"测试结果: {passed} 通过, {failed} 失败")
    print("=" * 60)
    
    if failed > 0:
        print("\n[WARNING] 部分模块导入失败，请检查:")
        print("  1. 是否安装了所有依赖: pip install -r requirements.txt")
        print("  2. 是否在正确的目录运行此脚本")
        print("  3. Python 路径是否正确")
        sys.exit(1)
    else:
        print("\n[SUCCESS] 所有模块导入成功！fastapi-infra 已就绪。")
        sys.exit(0)


if __name__ == "__main__":
    main()
