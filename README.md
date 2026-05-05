# FastAPI-Infra

高性能、生产级的 FastAPI 基础设施包

## 🚀 特性

**核心基础设施（100% 完成 ✅）**
- ✅ HTTP 客户端（连接池、弹性机制、性能追踪）
- ✅ 数据库管理（aiomysql + Redis，高性能连接池）
- ✅ 缓存服务（Redis 封装，命名空间隔离）
- ✅ 统一日志系统（结构化、trace_id、分布式追踪）
- ✅ 服务注册与依赖注入（DI 容器）
- ✅ 配置管理（跨平台 .env 支持）
- ✅ 统一异常体系（层次化、结构化错误）
- ✅ 中间件系统（请求日志、错误处理）
- ✅ 并发控制（全局线程池、装饰器）
- ✅ API 契约（统一响应、错误码、分页）
- ✅ 流式响应管理（Redis Streams、消息队列）
- ✅ 生命周期管理（启动/关闭回调）

**插件系统（计划中）**
- 🔄 分布式锁（Redis 实现）
- 🔄 事务协调器（Saga 补偿模式）
- 🔄 后台任务引擎（Celery 集成）

**可观测性（计划中）**
- 🔄 性能监控（Prometheus 指标）
- 🔄 分布式追踪（OpenTelemetry）
- 🔄 健康检查（数据库、缓存、外部服务）

## 📦 安装

### 方式 1：Git Submodule（推荐）

```bash
cd your-project
git submodule add https://github.com/your/fastapi-infra.git infra
pip install -r infra/requirements.txt
```

### 方式 2：直接克隆

```bash
git clone https://github.com/your/fastapi-infra.git
cd your-project
cp -r ../fastapi-infra/infra ./
pip install -r requirements.txt
```

### 方式 3：压缩包

1. 下载 [最新版本](https://github.com/your/fastapi-infra/releases)
2. 解压到项目目录
3. 安装依赖：`pip install -r requirements.txt`

## 🎯 快速开始

### 1. 创建配置文件

```python
# config.py
from infra.config import BaseSettings

class Settings(BaseSettings):
    app_name: str = "My App"
    mysql_host: str = "localhost"
    mysql_port: int = 3306
    mysql_user: str = "root"
    mysql_password: str = ""
    mysql_db: str = "mydb"
    redis_url: str = "redis://localhost:6379/0"

settings = Settings()
```

### 2. 创建应用

```python
# app.py
from fastapi import FastAPI
from infra.database import DatabaseManager
from infra.cache import CacheService
from infra.logging import get_logger
from config import settings

logger = get_logger(__name__)
app = FastAPI()

# 初始化基础设施
db = DatabaseManager(config=vars(settings))
cache = CacheService(namespace="myapp")

@app.on_event("startup")
async def startup():
    await db.initialize()
    logger.info("应用启动完成")

@app.on_event("shutdown")
async def shutdown():
    await db.close()
    logger.info("应用关闭完成")

@app.get("/")
async def root():
    return {"message": "Hello World"}
```

### 3. 启动服务

```bash
# 开发环境（热重载）
python start.py --reload

# 生产环境（多 worker）
python start.py --env production --workers 8
```

## 📖 核心组件使用

### HTTP 客户端

```python
from infra.http import HttpClient

client = HttpClient(timeout=10.0)
response = await client.get("https://api.example.com/data")
if response.is_success:
    data = response.json()
await client.close()
```

### 数据库操作

```python
from infra.database import BaseRepository

class UserRepository(BaseRepository):
    def __init__(self):
        super().__init__(table_name="users")
    
    async def find_by_email(self, email: str):
        return await self.find_by({"email": email})

# 使用
repo = UserRepository()
user = await repo.get_by_id("user_123")
```

### 缓存服务

```python
from infra.cache import CacheService

cache = CacheService(namespace="users")

# 设置缓存（1 小时）
await cache.set("user_123", user_data)

# 获取缓存
user = await cache.get("user_123")

# 删除缓存
await cache.delete("user_123")
```

### 日志系统

```python
from infra.logging import get_logger, set_log_context

logger = get_logger(__name__)

# 设置追踪上下文
set_log_context(trace_id="req-123", user_id="user-456")

logger.info("用户登录成功", extra={"ip": "1.2.3.4"})
logger.error("操作失败", exc_info=True)
```

## 📁 示例项目

- `examples/minimal/` - 最小化示例，快速上手
- `examples/full-featured/` - 完整功能示例（计划中）
- `examples/with-plugins/` - 插件使用示例（计划中）

## 📚 文档

详细文档见 `docs/` 目录：

- [快速开始](docs/getting-started.md)
- [核心组件](docs/core-components.md)
- [插件系统](docs/plugins.md)
- [最佳实践](docs/best-practices.md)

## 🤝 贡献

欢迎贡献代码、提交问题或改进建议！

## 📄 许可证

MIT License

## 🔖 版本

当前版本：**v0.1.0**

查看 [CHANGELOG.md](CHANGELOG.md) 了解版本历史。

---

**从 AI_Server 母包提取** | 为下一代项目提供标准化基础设施
