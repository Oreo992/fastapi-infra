# FastAPI Infra

一个干净的 FastAPI 基础设施层：核心只负责配置、生命周期、插件管理和健康聚合；AI、认证、支付、任务、存储等能力都作为可开关插件提供。

## 安装

```bash
pip install -e .
```

按需安装可选能力：

```bash
pip install -e ".[ai]"
pip install -e ".[mysql,redis,observability]"
```

## 快速开始

```python
from fastapi import FastAPI

from infra import InfraSettings, setup_infra

app = FastAPI(title="my app")
infra = setup_infra(app, InfraSettings())


@app.get("/health")
async def health():
    return {
        name: status.model_dump()
        for name, status in infra.health.snapshot().items()
    }
```

默认内置插件都是内存安全或 mock 实现，可以直接启动：

- `ai`
- `auth`
- `observability`
- `tasks`
- `storage`
- `webhooks`
- `payment`
- `ratelimit`
- `notifications`

`database`、`cache` 和 `http` 也在内置插件列表里，但默认关闭。需要真实
MySQL、Redis、HTTP 客户端时再通过 `enabled=True` 打开，并安装对应
optional dependencies。

## 可观测性路由

`observability` 插件会注册内存中的计数、耗时和健康状态服务，但 HTTP
路由需要显式安装：

```python
from infra.plugins.observability import install_observability_routes

install_observability_routes(app, infra, prefix="/ops")
```

上面的调用会添加 `/ops/healthz`、`/ops/readyz` 和 `/ops/metrics`。
`readyz` 只有在任一健康状态为 `unhealthy` 时返回 `503`，其他状态返回
`200`。`metrics` 使用简单的 `text/plain` 文本格式，例如
`requests_total 3`，适合没有接入 `prometheus_client` 的轻量场景。

## 动态开关

每个插件都通过 `InfraSettings.infra.plugins` 控制：

```python
settings = InfraSettings(
    infra={
        "plugins": {
            "payment": {"enabled": False},
            "ai": {
                "enabled": True,
                "config": {"default_provider": "mock"},
            },
        }
    }
)
```

`enabled` 的含义：

- `True`: 强制启用，配置或依赖错误会直接失败。
- `False`: 明确关闭，插件不会注册服务。
- `None`: 自动模式，适合可选依赖和默认能力。

## AI

AI 插件默认使用 mock provider，适合测试和本地开发：

```python
ai = infra.get("ai")
response = await ai.chat_text("hello")
```

SDK adapter 已提供 OpenAI、Anthropic、Gemini 的统一表层，按需安装：

```bash
pip install -e ".[ai-openai]"
pip install -e ".[ai-anthropic]"
pip install -e ".[ai-gemini]"
```

## 认证

`auth` 插件支持 API key、HS256 JWT 和基础 RBAC：

```python
settings = InfraSettings(
    infra={
        "plugins": {
            "auth": {
                "enabled": True,
                "config": {
                    "jwt_secret": "change-me",
                    "api_keys": {
                        "dev-key": {
                            "subject": "developer",
                            "scopes": ["checkout:create"],
                            "roles": ["admin"],
                        }
                    },
                },
            }
        }
    }
)
```

```python
auth = infra.get("auth")
token = auth.issue_jwt("user-1", scopes={"read:items"}, roles={"member"})
principal = auth.authenticate_bearer(f"Bearer {token}")
auth.require_roles(principal, ["member"])
```

## 支付

`payment` 插件现在注册 `PaymentService`，底层通过 provider registry 分发。
默认内置 `mock` provider，真实 Stripe/PayPal 等渠道后续可作为 provider 接入：

```python
payment = infra.get("payment")
checkout = await payment.create_checkout(
    amount=1250,
    currency="usd",
    reference="order-123",
)
status = await payment.get_payment_status(checkout.id)
```

## Redis 任务队列

`tasks` 默认是内存队列。需要跨进程任务分发时可以切换到 Redis Streams：

```python
settings = InfraSettings(
    infra={
        "plugins": {
            "database": {"enabled": True},
            "tasks": {
                "enabled": True,
                "config": {
                    "adapter": "redis",
                    "stream_name": "myapp:tasks",
                    "consumer_group": "workers",
                    "consumer_name": "worker-1",
                    "pending_min_idle_ms": 60000,
                },
            },
        }
    }
)
```

Redis adapter 会创建 consumer group，空队列时非阻塞返回 `None`，并优先恢复
超过 `pending_min_idle_ms` 的 pending 任务。

## 文档和示例

- [架构](docs/architecture.md)
- [插件系统](docs/plugins.md)
- [AI 插件](docs/ai.md)
- [最小示例](examples/minimal/app.py)
- [AI 示例](examples/ai_app/app.py)
- [全栈插件示例](examples/full_stack/app.py)
