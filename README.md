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

## 文档和示例

- [架构](docs/architecture.md)
- [插件系统](docs/plugins.md)
- [AI 插件](docs/ai.md)
- [最小示例](examples/minimal/app.py)
- [AI 示例](examples/ai_app/app.py)
- [全栈插件示例](examples/full_stack/app.py)
