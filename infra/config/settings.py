"""
系统配置管理

基于 Pydantic Settings 的类型安全配置管理
支持跨平台环境配置自动加载:
- Windows: 自动加载 .env.windows
- Linux: 自动加载 .env.linux
- 通用: 可以使用 .env 作为默认配置
"""

import logging
import os
import platform
from pathlib import Path
from typing import Literal

from pydantic import ConfigDict, Field
from pydantic_settings import BaseSettings as PydanticBaseSettings


# 配置日志
logger = logging.getLogger(__name__)


def get_platform_env_file() -> str:
    """
    根据操作系统和部署区域自动选择环境配置文件

    Returns:
        str: 环境配置文件路径

    规则:
    1. 优先检查环境变量 ENV_FILE 指定的配置文件
    2. Windows系统 -> .env.windows
    3. Linux系统:
       - 如果设置了 DEPLOYMENT_REGION=sea -> .env.linuxsea (海外)
       - 否则 -> .env.linux (国内)
    4. 如果平台特定文件不存在,回退到 .env
    """
    system = platform.system().lower()
    base_path = Path.cwd()  # 使用当前工作目录作为项目根目录

    # 1. 优先使用环境变量 ENV_FILE 指定的配置文件
    env_file_override = os.getenv("ENV_FILE")
    if env_file_override:
        override_path = base_path / env_file_override
        if override_path.exists():
            logger.info(f"使用环境变量指定的配置文件: {env_file_override}")
            return str(override_path)
        else:
            logger.warning(f"环境变量指定的配置文件 {env_file_override} 不存在")

    # 2. 根据平台和部署区域选择配置文件
    deployment_region = os.getenv("DEPLOYMENT_REGION", "").lower()

    # 平台映射
    if system == "windows":
        platform_env = ".env.windows"
    elif system in ["linux", "darwin"]:  # macOS 使用 Linux 配置
        if deployment_region == "sea":
            platform_env = ".env.linuxsea"  # 海外环境
        else:
            platform_env = ".env.linux"  # 国内环境（默认）
    else:
        platform_env = ".env"

    platform_env_path = base_path / platform_env
    default_env_path = base_path / ".env"

    # 检查文件是否存在
    if platform_env_path.exists():
        logger.info(f"使用平台特定配置文件: {platform_env}")
        return str(platform_env_path)
    elif default_env_path.exists():
        logger.warning(f"平台配置文件 {platform_env} 不存在,使用默认配置 .env")
        return str(default_env_path)
    else:
        logger.warning("未找到任何环境配置文件,使用默认值")
        return ""


class BaseSettings(PydanticBaseSettings):
    """
    基础配置类 - 供项目继承使用
    
    使用示例:
        class MySettings(BaseSettings):
            # 添加项目特定配置
            my_custom_field: str = "value"
        
        settings = MySettings()
    """

    # ==================== 应用基础配置 ====================
    app_name: str = Field(default="FastAPI App", description="应用名称")
    app_version: str = Field(default="1.0.0", description="应用版本")
    debug: bool = Field(default=False, description="调试模式")
    environment: Literal["development", "testing", "production"] = Field(
        default="development", description="运行环境"
    )

    # ==================== 服务器配置 ====================
    host: str = Field(default="0.0.0.0", description="服务器主机")
    port: int = Field(default=8000, description="服务器端口")

    # ==================== 数据库配置 ====================
    mysql_host: str = Field(default="localhost", description="MySQL 主机")
    mysql_port: int = Field(default=3306, description="MySQL 端口")
    mysql_user: str = Field(default="root", description="MySQL 用户名")
    mysql_password: str = Field(default="", description="MySQL 密码")
    mysql_db: str = Field(default="test", description="数据库名称")

    # ==================== Redis 配置 ====================
    redis_url: str = Field(
        default="redis://localhost:6379/0", description="Redis 连接 URL"
    )

    # ==================== 日志配置 ====================
    log_level: str = Field(default="INFO", description="日志级别")
    log_format: Literal["json", "pretty"] = Field(
        default="pretty", description="日志格式"
    )

    # ==================== 安全配置 ====================
    secret_key: str = Field(
        default="change-this-in-production",
        description="应用密钥（生产环境务必修改）",
    )

    # ==================== Pydantic 配置 ====================
    model_config = ConfigDict(
        env_file=get_platform_env_file(),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="allow",  # 允许额外字段（子类扩展）
    )

    # ==================== 辅助方法 ====================
    @property
    def env_suffix(self) -> str:
        """获取环境后缀，用于隔离测试/开发数据

        - production: 无后缀
        - testing: _test
        - development: _dev
        """
        if self.environment == "production":
            return ""
        elif self.environment == "testing":
            return "_test"
        else:
            return "_dev"

    def get_table_name(self, base_name: str) -> str:
        """根据环境获取数据库表名

        Args:
            base_name: 基础表名，如 "users"

        Returns:
            带环境后缀的表名，如 "users_test"
        """
        return f"{base_name}{self.env_suffix}"


# 导出默认配置实例（可选）
# settings = BaseSettings()
