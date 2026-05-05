"""启动管理模块

应用生命周期管理：
- LifecycleManager: 生命周期管理器
- create_lifecycle_manager: 快捷创建函数
"""

from infra.startup.lifecycle import (
    LifecycleManager,
    create_lifecycle_manager,
)

__all__ = [
    "LifecycleManager",
    "create_lifecycle_manager",
]
