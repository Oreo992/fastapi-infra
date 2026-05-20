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

先选一个组件组合生成新项目：

```bash
fastapi-infra profiles
fastapi-infra new services/billing_api --profile saas
cd services/billing_api
pip install -e ".[dev]"
fastapi-infra config-check --settings infra.toml
fastapi-infra project-check .
python -m pytest -q
uvicorn app.main:app --reload
curl http://127.0.0.1:8000/health
```

`--profile minimal` 生成最小项目，所有插件显式关闭；`api`、`worker`、`ai`、
`saas` 和 `full` 则对应常见业务基础设施组合。`--plugins` 用来在 profile
基础上追加单个组件，例如：

```bash
fastapi-infra new services/agent_api --profile api --plugins ai,speech
```

不传 `--profile` 时默认使用 `minimal`。脚手架会
始终生成 `app/main.py`、`app/settings.py`、`tests/test_config.py`、
`tests/test_health.py`、`Dockerfile`、`Makefile`、`compose.yaml`、`.dockerignore`、`.gitignore`、
`.github/workflows/ci.yml`、`AGENTS.md`、`README.md`、`.env.example`、`provider.env.example`、`infra.toml`、
`infra.production.example.toml`、`scripts/prepare-env.sh`、`scripts/verify-release.sh`
和 `infra.manifest.json`。`infra.manifest.json` 记录 profile、显式请求插件、
生产 profile 插件、推荐命令和插件服务/env 摘要；`fastapi-infra project-check .`
会校验这份清单与实际文件、`infra.toml`、`infra.production.example.toml`、
Makefile、Dockerfile 和 CI workflow 是否一致，适合放进 CI，也适合让 AI agent
快速理解项目边界。

查看可用插件、服务名、依赖和配置 schema：

```bash
fastapi-infra plugins
fastapi-infra plugins --json
fastapi-infra plugins check ai --json
fastapi-infra profiles --json
fastapi-infra plugins --settings infra.toml --json
fastapi-infra config-check --settings infra.toml --json
fastapi-infra config-check --settings infra.production.example.toml --env-file .env --json
fastapi-infra project-check . --json
```

`plugins --json` 是给脚手架和 AI agent 使用的稳定发现入口，避免靠读源码猜
插件配置。它会输出配置 schema、推荐 extras、常用环境变量、local/production
配置示例、默认服务键 import path、按当前 settings 解析出的服务名、配置里的服务
引用字段和 release-check 注意事项。服务引用可以声明机器可读的
`required_when_config` / `required_unless_config` 条件，所以第三方插件不需要把
特殊规则写进中心校验器。`config-check` 会在不启动应用的情况下校验
已启用插件的配置 schema、manifest 服务引用声明和关键服务引用，用于提前发现
provider 参数拼错、未知插件名、`payment.store_service` 或 Redis 后端
`database_service` 指向未启用服务等问题。配置文件使用 `{ "$env" = "NAME" }`
引用时，可传 `--env-file` 临时加载本地 `.env` 或 CI 生成的凭据文件。

创建外部插件包骨架：

```bash
fastapi-infra plugins init search services/search_plugin
cd services/search_plugin
pip install -e ".[dev]"
python -m pytest -q
fastapi-infra plugins check search --settings infra.example.toml --lifecycle
```

生成的插件包会包含 `pyproject.toml` entry point、插件实现、manifest hints、
`infra.example.toml` 和 conformance 测试，后续可以直接通过
`fastapi-infra new /tmp/search-api --plugins search` 验证脚手架链路。
如果只是在扩展内置插件的厂商适配器，而不是新增完整服务插件，可以生成 provider
包。当前模板覆盖 AI、支付、语音、存储、通知、webhook、tasks backend 和 ratelimit backend，适合 OpenRouter、自建网关、
私有模型服务，Adyen、PayPal、Paddle 这类支付渠道，以及 Deepgram、ElevenLabs
这类 ASR/TTS 服务、S3/R2/OSS/MinIO 这类对象存储、Twilio/SendGrid/飞书/Slack
这类通知渠道，GitHub、Stripe、LemonSqueezy 这类入站事件源，以及 SQS、Celery、
Kafka 这类任务队列后端、Upstash/Cloudflare KV 这类限流后端接入：

```bash
fastapi-infra plugins init openrouter providers/openrouter --kind provider --provider-kind ai
fastapi-infra plugins init adyen providers/adyen --kind provider --provider-kind payment
fastapi-infra plugins init deepgram providers/deepgram --kind provider --provider-kind speech
fastapi-infra plugins init r2 providers/r2 --kind provider --provider-kind storage
fastapi-infra plugins init twilio providers/twilio --kind provider --provider-kind notifications
fastapi-infra plugins init github providers/github --kind provider --provider-kind webhook
fastapi-infra plugins init sqs providers/sqs --kind provider --provider-kind tasks
fastapi-infra plugins init upstash providers/upstash --kind provider --provider-kind ratelimit
cd providers/openrouter
pip install -e ".[dev]"
python -m pytest -q
fastapi-infra config-check --settings infra.example.toml
```

本仓库本身的本地门禁可以用一个入口跑，避免手动漏掉类型、格式或测试：

```bash
python scripts/verify_local.py
python scripts/verify_local.py --package --smoke
python scripts/verify_local.py --package --smoke --dist-dir dist
```

默认的 package 验证会把 wheel/sdist 构建到临时目录，避免仓库里的旧 `dist/`
产物影响验证结果。只有准备保留本次验证过的 release artifact 时才传
`--dist-dir`；该目录必须为空，否则脚本会拒绝继续。

provider 包会暴露对应的 `fastapi_infra.ai_providers` 或
`fastapi_infra.payment_providers`、`fastapi_infra.speech_providers`、
`fastapi_infra.storage_providers`、`fastapi_infra.notification_providers` 或
`fastapi_infra.webhook_providers`，或 `fastapi_infra.task_queue_backends`、
`fastapi_infra.ratelimit_backends`，以及 `fastapi_infra.provider_checks` entry
point；应用只需要启用内置 `ai`、`payment`、`speech`、`storage`、
`notifications`、`webhooks`、`tasks` 或 `ratelimit` 插件并配置对应 provider/backend 名称。
维护模板时可以跑全量模板烟测，它会生成 service 插件和所有 provider/backend 包，
把每个包 editable 安装到 smoke work-dir 私有 target、跑包内测试，再跑
`plugins check` 或 `config-check`：

```bash
python scripts/smoke_plugin_templates.py --work-dir /tmp/fastapi-infra-plugin-template-smoke
```

业务路由需要直接使用某个插件服务时，可以用通用依赖函数，避免每个项目重复读取
`request.app.state.infra`：

```python
from typing import Annotated

from fastapi import Depends
from infra import infra_service
from infra.plugins import PAYMENT_SERVICE
from infra.plugins.payment import PaymentService

@app.post("/checkout")
async def checkout(
    payment: Annotated[PaymentService, Depends(infra_service(PAYMENT_SERVICE))],
):
    ...
```

普通应用代码中需要强制取得服务时，可以用 `infra.require(PAYMENT_SERVICE)`；
缺失服务会统一抛出 `RuntimeError`，类型不匹配会由 `ServiceKey` 校验出来。
可选服务继续用 `infra.get(PAYMENT_SERVICE)` 或
`infra_service(PAYMENT_SERVICE, default=...)`。`infra.plugins` 提供内置插件默认
服务键，例如 `AI_SERVICE`、`AUTH_SERVICE`、`DATABASE_SERVICE`、
`HTTP_SERVICE`、`TASKS_SERVICE`、`STORAGE_SERVICE` 和 `WEBHOOKS_SERVICE`。

```python
from fastapi import FastAPI

from infra import InfraSettings, setup_infra

app = FastAPI(title="my app")
infra = setup_infra(
    app,
    InfraSettings(),
    health_check_timeout_seconds=5,
)


@app.get("/health")
async def health():
    return {
        name: status.model_dump()
        for name, status in infra.health.snapshot().items()
    }
```

核心默认不启动任何内置插件。所有能力都通过 `enabled=True` 显式打开，
这样新项目可以先保持最小运行面，再按业务需要启用 AI、认证、支付、任务、
存储、通知、可观测性等组件。需要外部依赖的插件还要安装对应 optional
dependencies，例如 `fastapi-infra[mysql,redis]` 或 `fastapi-infra[http]`。
HTTP 插件的 `base_url` 可以为空或绝对 `http`/`https` URL，`timeout`
必须大于 0；默认 headers 会按敏感配置处理，因为通常会承载 Authorization token。
底层 `HttpClient` 还提供可选 `HttpRetryConfig`：默认只重试幂等方法
`GET`、`HEAD`、`OPTIONS`、`PUT`、`DELETE`，且只针对 `429/5xx` 等临时状态
或超时/连接错误；`POST`、`PATCH` 需要调用方显式开启 `retry_all_methods`。
如果同时启用了 `observability` 和 `http` 插件，HTTP 客户端会自动记录
`http_client_requests_total`、`http_client_attempts_total`、
`http_client_responses_total`、`http_client_errors_total`、
`http_client_request_seconds` 等指标，并传播当前日志上下文中的
`X-Trace-ID` 和 `X-Request-ID`。显式传入的同名请求头不会被覆盖。

跨系统业务编排可以使用 `TransactionCoordinator` 的 Saga 补偿模式。它会返回每步
执行结果、失败步骤、已补偿步骤和补偿失败列表，适合支付、库存、通知这类不能放进
同一个数据库事务的流程。Redis `DistributedLockManager` 是 lease lock：支持
`SET NX EX`、token 校验释放和延期，但不提供 fencing token；强一致写保护仍应在
业务存储层配合版本号或条件更新。

## API 契约

业务接口需要统一响应形状时，使用 `ApiResponse`，不要再引入第二套
`StandardResponse` 风格：

```python
from infra.common import ApiResponse, ErrorCode, PaginatedResponse, PaginationParams

params = PaginationParams(page=2, size=20)
page = PaginatedResponse.create(items=[{"id": "invoice-1"}], total=41, page=2, size=20)

return ApiResponse.ok(page, trace_id="trace-123")

error = ApiResponse.fail(ErrorCode.NOT_FOUND, "invoice not found", trace_id="trace-123")
```

## 可观测性路由

`observability` 插件会注册内存中的计数、耗时和健康状态服务，但 HTTP
路由需要显式安装：

```python
from infra.plugins.observability import install_observability_routes

install_observability_routes(app, infra, prefix="/ops")
```

上面的调用会添加 `/ops/healthz`、`/ops/readyz` 和 `/ops/metrics`。
`healthz` 返回当前缓存的健康快照；`readyz` 会先刷新已启用插件的健康检查，
并发执行每个插件探测，单插件默认 5 秒超时；可用
`readiness_timeout_seconds` 调整。只有在任一状态为 `unhealthy` 时返回
`503`，其他状态返回 `200`。
`metrics` 使用 Prometheus text exposition 格式，例如
`# TYPE requests_total counter` 和 `requests_total 3`，适合没有接入
`prometheus_client` 的轻量场景。

生产环境如果需要标准 Prometheus client registry，安装
`fastapi-infra[observability]` 并配置：

```python
settings = InfraSettings(
    infra={
        "plugins": {
            "observability": {
                "enabled": True,
                "config": {
                    "metrics_backend": "prometheus",
                    "tracing_backend": "opentelemetry",
                },
            }
        }
    }
)
```

`tracing_backend="opentelemetry"` 使用进程里已配置的 OpenTelemetry tracer
provider；基础设施层不绑定具体 exporter。

请求指标需要显式安装 middleware：

```python
from infra.plugins.observability import install_observability_middleware

install_observability_middleware(app)
```

它会记录真实请求数、状态码计数、请求耗时和异常计数。

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
- `None`: 使用插件元数据默认值；内置插件默认都是关闭。

也可以从 JSON/TOML 文件和环境变量加载配置：

```python
from infra.config import load_infra_settings

settings = load_infra_settings("infra.toml")
```

环境变量使用双下划线路径，并覆盖文件配置：

```bash
INFRA__INFRA__PLUGINS__PAYMENT__ENABLED=false
INFRA__INFRA__PLUGINS__AUTH__CONFIG__JWT_SECRET=change-me
```

配置文件里的 secret 可以引用进程环境变量，loader 会在插件校验前解析，变量缺失
会直接失败：

```toml
[infra.plugins.payment.config.providers.stripe]
api_key = { "$env" = "STRIPE_API_KEY" }
webhook_secret = { "$env" = "STRIPE_WEBHOOK_SECRET" }
```

`{"$env": "NAME"}` 只支持必填环境变量，不做默认值逻辑；可选值保持不配置，
或继续用双下划线环境变量覆盖。

## AI

AI 插件启用后默认使用 mock provider，适合测试和本地开发：

```python
from infra.plugins import AI_SERVICE

ai = infra.require(AI_SERVICE)
response = await ai.chat_text("hello")
```

SDK adapter 已提供 OpenAI、Anthropic、Gemini 的统一表层，按需安装：

```bash
pip install -e ".[ai-openai]"
pip install -e ".[ai-anthropic]"
pip install -e ".[ai-gemini]"
```

Provider 可以显式配置 SDK 参数，不必只依赖环境变量：

```python
settings = InfraSettings(
    infra={
        "plugins": {
            "ai": {
                "enabled": True,
                "config": {
                    "default_provider": "openai",
                    "health_probe": True,
                    "providers": {
                        "openai": {
                            "api_key": "sk-...",
                            "base_url": "https://api.openai.com/v1",
                            "timeout": 10,
                        }
                    },
                },
            }
        }
    }
)
```

默认健康检查不会主动访问 AI 厂商；生产环境可以设置 `health_probe=True`，
让健康检查通过 SDK 调用模型列表接口，验证凭据和上游连通性。

OpenAI 和 Gemini provider 支持 embeddings；不支持 embeddings 的 provider 会明确抛出
`NotImplementedError`，不会返回占位向量：

```python
from infra.plugins.ai import EmbeddingRequest

embedding = await ai.embed(
    EmbeddingRequest(model="text-embedding-3-small", input="hello"),
    provider="openai",
)
```

## 语音

`speech` 插件提供 ASR/TTS 的 provider 架构，默认 mock provider 不依赖外部 SDK：

```python
from infra.plugins import SPEECH_SERVICE

speech = infra.require(SPEECH_SERVICE)
transcription = await speech.transcribe(b"audio")
synthesis = await speech.synthesize("hello")
```

需要真实 OpenAI ASR/TTS 时，启用 `openai` provider。它使用 stdlib HTTP
实现，不额外引入 SDK；缺少 `api_key` 会启动失败：

```python
settings = InfraSettings(
    infra={
        "plugins": {
            "speech": {
                "enabled": True,
                "config": {
                    "default_provider": "openai",
                    "providers": {
                        "openai": {
                            "api_key": "sk-...",
                            "asr_model": "gpt-4o-mini-transcribe",
                            "tts_model": "gpt-4o-mini-tts",
                            "voice": "alloy",
                            "tts_response_format": "mp3",
                            "timeout": 60.0,
                            "max_attempts": 3,
                            "retry_base_delay": 0.25,
                        }
                    },
                },
            }
        }
    }
)
```

`timeout` 控制 OpenAI speech ASR/TTS HTTP 请求的最大等待秒数，默认 `60.0`。
OpenAI Speech provider 会对 `408`、`409`、`429` 和 `5xx` 响应以及 transport
错误按指数退避重试；`max_attempts` 默认 `3`，`retry_base_delay` 默认 `0.25` 秒。
如果上游返回 `Retry-After`，重试会优先使用 provider 指定的等待时间。
自定义 `api_base` 必须是绝对 `http` 或 `https` URL。
默认健康检查不会主动访问 OpenAI；生产环境可以设置 `health_probe=True`，
让健康检查查询配置的 ASR/TTS 模型是否可用。

## 认证

`auth` 插件支持哈希 API key、HS256 JWT 和基础 RBAC。API key 只接受
`hashed_api_keys` 配置，使用 PBKDF2-HMAC-SHA256 存储；不提供明文
`api_keys` 配置路径，避免测试便利变成生产风险。

```python
settings = InfraSettings(
    infra={
        "plugins": {
            "auth": {
                "enabled": True,
                "config": {
                    "jwt_secret": "production-jwt-secret-at-least-32-chars",
                    "jwt_signing_keys": {
                        "previous": {"secret": "previous-jwt-secret-at-least-32-chars"},
                        "current": {"secret": "current-jwt-secret-at-least-32-chars"},
                    },
                    "jwt_key_id": "current",
                    "hashed_api_keys": {
                        "primary": {
                            "key_hash": "pbkdf2_sha256$260000$...",
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

可以用 `infra.plugins.auth.hash_api_key("real-api-key")` 生成配置中的
`key_hash`；轮换时添加新的 `hashed_api_keys` 记录，待旧调用方迁移后移除旧记录。
`jwt_secret` 是单 key 简写。生产环境可以使用 `jwt_signing_keys` 和
`jwt_key_id`：新签发的 JWT 会带 `kid`，验证时接受所有已配置 key，方便先部署新
key，再移除旧 key。
生产发布检查会拒绝短 JWT secret、常见占位值，以及不符合
`pbkdf2_sha256$iterations$salt$hash` 格式或迭代次数过低的 API key hash。

```python
from infra.plugins import AUTH_SERVICE

auth = infra.require(AUTH_SERVICE)
token = auth.issue_jwt("user-1", scopes={"read:items"}, roles={"member"})
principal = auth.authenticate_bearer(f"Bearer {token}")
auth.require_roles(principal, ["member"])
```

业务路由可以直接使用 FastAPI dependency：

```python
from typing import Annotated

from fastapi import Depends
from infra.plugins.auth import Principal, require_scopes


@app.get("/items")
async def list_items(
    principal: Annotated[Principal, Depends(require_scopes("items:read"))],
):
    return {"subject": principal.subject}
```

## 支付

`payment` 插件现在注册 `PaymentService`，底层通过 provider registry 分发。
默认内置 `mock` provider，适合本地开发和测试。真实 Stripe provider
使用 Stripe Checkout Sessions API，并要求显式配置 `api_key`；配置缺失会启动失败，
不会静默降级到 mock：

```python
settings = InfraSettings(
    infra={
        "plugins": {
            "payment": {
                "enabled": True,
                "config": {
                    "default_provider": "stripe",
                    "health_probe": True,
                    "providers": {
                        "stripe": {
                            "api_key": "sk_live_...",
                            "webhook_secret": "whsec_...",
                            "timeout": 30.0,
                            "max_attempts": 3,
                            "retry_base_delay": 0.25,
                        }
                    },
                },
            }
        }
    }
)
```

`timeout` 控制 Stripe API 请求的最大等待秒数，默认 `30.0`。Stripe provider
会对 `409`、`429` 和 `5xx` 响应以及 transport 错误按指数退避重试；
`max_attempts` 默认 `3`，`retry_base_delay` 默认 `0.25` 秒。`4xx` 业务错误不会
重试，会作为结构化 `StripeAPIError` 抛出。自定义 `api_base` 必须是绝对
`http` 或 `https` URL。
默认健康检查不会主动访问外部网络；生产环境可以设置 `health_probe=True`，
让健康检查调用 Stripe `/v1/account` 验证 API key 和上游连通性。
`create_checkout` 和 `create_refund` 在传入 `reference` 且没有显式
`provider_options["idempotency_key"]` 时，会自动派生稳定的 Stripe
`Idempotency-Key`；复杂业务仍可显式传入自己的 key。

```python
from infra.plugins import PAYMENT_SERVICE

payment = infra.require(PAYMENT_SERVICE)
checkout = await payment.create_checkout(
    amount=1250,
    currency="usd",
    reference="order-123",
    success_url="https://example.com/success",
    cancel_url="https://example.com/cancel",
)
status = await payment.get_payment_status(checkout.id)
refund = await payment.create_refund(
    checkout_id=checkout.id,
    amount=1250,
    currency="usd",
    provider_options={"payment_intent": "pi_...", "idempotency_key": "refund-order-123"},
)
```

需要持久化 provider 结果时，可以显式配置 `store_service`。它只记录
checkout/refund 的 provider 状态，不替代业务订单表，也不会让 payment 默认强依赖
database。生产发布门禁会要求 `store_service="database"` 时 database 插件启用
MySQL，并且 Stripe live certification 会连带要求 MySQL certification 通过：

```python
settings = InfraSettings(
    infra={
        "plugins": {
            "database": {"enabled": True},
            "payment": {
                "enabled": True,
                "config": {"default_provider": "stripe", "store_service": "database"},
            },
        }
    }
)
```

Stripe webhook 签名校验使用 `t=...,v1=...` HMAC SHA256 规则：

```python
from infra.plugins.payment import verify_webhook_signature

ok = verify_webhook_signature(payload, stripe_signature, "whsec_...")
```

需要接收 inbound webhook 时，可以安装真实 POST 路由，使用 raw body 校验签名，
并按 provider + event id 做幂等去重。默认 store 是内存实现，只适合本地开发；
生产环境应在 webhooks 插件配置中声明 `durable_store` 和 `signature_verification`，
并用 `verified_providers` 声明必须安装签名校验器的 provider；如果声明了生产要求
但没有传入对应 provider 的持久化 store 和签名校验器，路由安装会直接失败：

```python
from infra.plugins.webhooks import (
    SqlWebhookStore,
    WebhookSignatureVerifierRegistry,
    install_webhook_routes,
    stripe_signature_verifier,
)

from infra.plugins import DATABASE_SERVICE, WEBHOOKS_SERVICE

database = infra.require(DATABASE_SERVICE)
dispatcher = infra.require(WEBHOOKS_SERVICE)
signature_verifiers = WebhookSignatureVerifierRegistry(
    {"stripe": stripe_signature_verifier("whsec_...")}
)
install_webhook_routes(
    app,
    dispatcher,
    store=SqlWebhookStore(database),
    signature_verifiers=signature_verifiers,
)
```

## 存储

`storage` 插件启用后默认注册本地文件系统 provider，并提供统一对象接口：

```python
from infra.plugins import STORAGE_SERVICE

storage = infra.require(STORAGE_SERVICE)
await storage.put_object("invoices/1.json", b"{}")
data = await storage.get_object("invoices/1.json")
```

需要 S3-compatible 对象存储时，切换 `default_provider`：

```python
settings = InfraSettings(
    infra={
        "plugins": {
            "storage": {
                "enabled": True,
                "config": {
                    "default_provider": "s3",
                    "health_probe": True,
                    "providers": {
                        "s3": {
                            "bucket": "my-bucket",
                            "region": "us-east-1",
                            "access_key_id": "...",
                            "secret_access_key": "...",
                            "timeout": 30.0,
                            "max_attempts": 3,
                            "retry_base_delay": 0.25,
                        }
                    },
                },
            }
        }
    }
)
```

S3 provider 使用 stdlib HTTP 客户端和 AWS Signature V4，不引入 `boto3`。
`timeout` 控制每次 HTTP 请求的最大等待秒数，默认 `30.0`，避免外部对象存储
故障时无限阻塞。S3 provider 会对 `409`、`429` 和 `5xx` 响应以及 transport
错误按指数退避重试；`max_attempts` 默认 `3`，`retry_base_delay` 默认 `0.25` 秒。
默认健康检查不会主动访问 S3；生产环境可以设置 `health_probe=True`，
让健康检查对 bucket 执行签名 `HEAD` probe。

## 通知

`notifications` 插件启用后默认使用 `noop` provider，仅用于本地开发和测试。需要真实邮件发送时，启用 SMTP provider：

```python
settings = InfraSettings(
    infra={
        "plugins": {
            "notifications": {
                "enabled": True,
                "config": {
                    "default_provider": "smtp",
                    "health_probe": True,
                    "providers": {
                        "smtp": {
                            "host": "smtp.example.com",
                            "port": 587,
                            "sender": "noreply@example.com",
                            "username": "mailer",
                            "password": "...",
                            "use_tls": True,
                            "timeout": 30.0,
                            "max_attempts": 3,
                            "retry_base_delay": 0.25,
                        }
                    },
                },
            }
        }
    }
)
```

默认健康检查不会主动连接 SMTP；生产环境可以设置 `health_probe=True`，
让健康检查执行连接、TLS 和登录 probe，但不会发送邮件。SMTP provider 会对临时连接
错误和临时 SMTP 响应码按指数退避重试；`max_attempts` 默认 `3`，
`retry_base_delay` 默认 `0.25` 秒。`port`、`timeout` 和 retry 参数会在启动前
校验。认证失败、发件人或收件人被拒绝不会重试。

SMTP 是真实外部 provider；当前 health 会报告 `degraded`，表示配置存在但
未做 SMTP 上游探测。是否真的可发送邮件由 live provider certification 验证。

需要把通知推送到内部系统或第三方 webhook 时，可以使用通用 webhook provider：

```python
settings = InfraSettings(
    infra={
        "plugins": {
            "notifications": {
                "enabled": True,
                "config": {
                    "default_provider": "webhook",
                    "health_probe": True,
                    "providers": {
                        "webhook": {
                            "url": "https://hooks.example.com/notify",
                            "health_url": "https://hooks.example.com/health",
                            "signing_secret": "...",
                            "timeout": 10.0,
                        }
                    },
                },
            }
        }
    }
)
```

Webhook provider 会以 JSON POST 发送通知内容；配置 `signing_secret` 后会附加
`x-infra-timestamp` 和 `x-infra-signature` HMAC-SHA256 签名。生产发布检查会要求
webhook 通知配置 `signing_secret`、`health_url` 和 `health_probe=True`。

## Redis 任务队列

`tasks` 插件启用后默认是内存队列。需要跨进程任务分发时可以切换到 Redis Streams：

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
超过 `pending_min_idle_ms` 的 pending 任务。Tasks 健康检查会确认注册队列存在；
Redis 队列会执行 `PING`，Redis 不可用时插件会标记为 unhealthy。
文件配置只描述 adapter 和 stream 参数；Redis client 是运行时对象，测试或高级嵌入
场景可以通过 `TasksPlugin(redis=client)` 注入。生产发布检查无法从配置文件证明
这种运行时注入，因此 Redis adapter 生产配置应启用 database 插件并保持
`redis_enabled=true`。provider certification 报告也必须覆盖 `redis`。

任务 worker 可以直接绑定 handler。生产进程建议使用 `run_task_worker`，它会安装
SIGINT/SIGTERM 停机处理，并在启动时拒绝没有 handler 的 worker：

```python
from infra.plugins.tasks import (
    TaskEnvelope,
    TaskWorker,
    TaskWorkerRunConfig,
    run_task_worker,
)
from infra.plugins import OBSERVABILITY_SERVICE, TASKS_SERVICE

queue = infra.require(TASKS_SERVICE)
task = await queue.enqueue(
    "send_email",
    {"to": "user@example.com"},
    idempotency_key="email:user-123:welcome",
    delay_seconds=30,
    max_attempts=3,
)
observability = infra.get(OBSERVABILITY_SERVICE)
worker = TaskWorker(queue, retry_backoff=2, instrumentation=observability)


@worker.handler("send_email")
async def send_email(task: TaskEnvelope) -> None:
    ...


stats = await run_task_worker(
    worker,
    TaskWorkerRunConfig(idle_sleep=0.5, require_handlers=True, concurrency=4),
)
```

`run_once()` 适合测试和短生命周期 worker。`TaskWorker.run()` 支持
`max_tasks`、`idle_poll_limit` 和 `concurrency`，便于批处理、健康探测式 worker
或并发 worker 返回可观察的 `processed`、`completed`、`retried`、
`dead_lettered`、`idle_polls` 和 `stopped` 统计。`enqueue()` 支持
`idempotency_key` 和 `delay_seconds`，用于防重复提交和延迟首发投递；重复
`idempotency_key` 会返回原任务，不会再次发布。任务会记录 `attempts`、
`max_attempts`、`idempotency_key` 和 `available_at`；handler 异常会按 `retry_backoff` 重新排队，
耗尽次数后进入 `dead_lettered`。未知任务名会直接进入 dead-letter，避免任务
卡在 `running`。传入 `instrumentation` 后，worker 会记录
`task_worker_tasks_total`、`task_worker_completed_total`、
`task_worker_retried_total`、`task_worker_dead_lettered_total`、
`task_worker_idle_polls_total` 和 `task_worker_task_seconds`。

## 限流

`ratelimit` 插件提供统一的 `allow(key, limit, window_seconds)` 接口。内存后端只适合
本地开发；生产环境应使用 Redis 后端，并启用 database 插件的 Redis 连接：

```python
settings = InfraSettings(
    infra={
        "plugins": {
            "database": {
                "enabled": True,
                "config": {"config": {"mysql_enabled": False, "redis_enabled": True}},
            },
            "ratelimit": {
                "enabled": True,
                "config": {"backend": "redis", "key_prefix": "myapp:ratelimit"},
            },
        }
    }
)

from infra.plugins import RATELIMIT_SERVICE

limiter = infra.require(RATELIMIT_SERVICE)
allowed = await limiter.allow("client:123", limit=100, window_seconds=60)
```

FastAPI 路由可以直接使用依赖函数：

```python
from fastapi import Depends

from infra.plugins.ratelimit import rate_limit


@app.get("/search", dependencies=[Depends(rate_limit(limit=60, window_seconds=60))])
async def search() -> dict[str, bool]:
    return {"ok": True}
```

默认 key 是客户端 IP。需要按账号、租户、API key 限流时传入 `key_func` 即可。被限流的请求会返回
`429`，并带上 `Retry-After`、`X-RateLimit-Limit`、`X-RateLimit-Window` 响应头。

Redis 后端使用 `INCR` 和 `EXPIRE` 做固定窗口计数。`release-check` 会拒绝生产配置里的
内存限流，并要求 Redis backing。

## 数据库迁移

启用 `database` 的新项目会生成 `migrations/` 目录。基础包提供轻量 SQL
migration 工具：

```bash
fastapi-infra migrations new migrations create_users
fastapi-infra migrations list migrations
fastapi-infra migrations migrate migrations --settings infra.toml
```

代码中可以用 `SqlMigrationRunner` 执行迁移，迁移记录写入
`infra_schema_migrations`，已应用文件的 checksum 变化会被拒绝。
生产部署可以显式传入 `lock` 和 `transaction_factory`，让整轮 migration
在互斥锁内运行，并把每个 migration 的 SQL 和版本记录放在同一个事务 executor
里执行。默认不做隐式探测，避免不同数据库驱动下出现不透明行为。

`DatabaseManager` 的 MySQL 和 Redis 连接可以独立启用：

```python
mysql_only = DatabaseManager({"mysql_enabled": True, "redis_enabled": False})
redis_only = DatabaseManager({"mysql_enabled": False, "redis_enabled": True})
```

## 项目脚手架

需要快速开一个业务项目时，可以用 CLI 或 Python API 生成最小 FastAPI app：

```bash
fastapi-infra profiles
fastapi-infra new services/billing_api --profile saas
fastapi-infra project-check services/billing_api
fastapi-infra migrations new services/billing_api/migrations create_orders
```

```python
from infra.scaffold import create_project

create_project(
    "services/billing-api",
    "billing_api",
    profile="api",
    enabled_plugins=("payment", "tasks"),
)
```

脚手架生成的 `app/main.py` 默认调用 `install_error_handlers()`，并安装
`ErrorHandlingMiddleware`、`RequestLoggingMiddleware` 和
`SecurityHeadersMiddleware`，因此新项目开箱就有统一错误响应、FastAPI
参数校验错误格式化、`X-Trace-ID` / `X-Request-ID` 透传，以及基础安全响应头。
按插件生成入口代码：启用 `observability` 时会安装
`/ops/healthz`、`/ops/readyz`、`/ops/metrics` 和请求指标 middleware；未启用
时不会导入 observability helper。启用 `tasks` 时会生成 `app/worker.py`，
其中包含基于 `TaskWorker` 和 infra context 的可运行 worker 入口，以及
`example.ping` handler 注册位置。启用 `database` 时会生成 `migrations/`
目录，并在项目 README 中列出创建、查看和执行 migration 的命令。
生成的 `infra.toml` 会为已启用插件写入可通过 `config-check` 的本地配置，
例如 `auth` 的 dev JWT secret、`tasks` 的 memory adapter、`storage` 的 local
root，以及 mock/noop provider 的本地默认值；`infra.production.example.toml`
会为同一批插件写入 production provider 示例，并使用 `{ "$env" = "..." }`
引用 `.env.example` 中列出的环境变量。
生成的 `tests/test_health.py` 同时验证 `/health`、trace/request header 透传
和已启用插件的 service wiring，避免脚手架项目只通过空的 health smoke test。
生成的 `tests/test_config.py` 会加载 `infra.toml` 并调用
`validate_infra_settings()`，同时执行 `fastapi-infra config-check --settings
infra.toml`，保证本地配置文件和 CLI 自检路径都先过静态校验。
生成的 `infra.manifest.json` 是脚手架项目的机器可读说明书：它列出 profile、
显式请求插件、启用插件、生产插件、打包插件、关键文件和标准验证命令。
`fastapi-infra project-check .` 会读取这份清单，检查必需文件是否存在、
`AGENTS.md` 是否把 agent 指向 manifest、Makefile 和 env 分离规则、
manifest 中的 `commands` 是否仍指向 `make env`、`make verify`、`make release-static`
等标准入口、
manifest 中的 `package_plugins` 和 `plugins` 摘要是否与 enabled/production 插件一致、
`pyproject.toml` 的 `fastapi-infra[...]` extras 是否覆盖这些插件的推荐依赖、
Makefile 是否提供 `make env`、`make verify`、`make release-static`、`make provider-preflight`
和 `make dev-up` 等统一入口、`.github/workflows/ci.yml` 是否调用这些 Makefile
门禁、`scripts/verify-release.sh` 是否复用 Makefile 门禁并保留 provider certification
流程、生成项目 smoke 是否把外部插件安装到 work-dir 私有 target、用测试占位值补齐 `provider.env` 后直接执行 release 脚本、Dockerfile 是否复制 `infra.manifest.json` 和
`scripts/`、是否使用非 root 用户、是否带 `/health` 容器健康检查、
`.dockerignore`/`.gitignore` 是否排除 `.env`、`provider.env`、provider env 模板和 provider 认证输出、
`compose.yaml` 是否按 production config 连接 MySQL/Redis、`scripts/prepare-env.sh`
是否生成安全 JWT_SECRET、`scripts/verify-release.sh` 是否可执行、
local/production 配置启用的插件是否与清单一致，以及生产 database
profile 是否带有 migrations 目录并被 Dockerfile 复制进镜像。

`observability` 默认使用内存指标并输出 Prometheus text exposition。需要标准
`prometheus-client` registry 时安装 `fastapi-infra[observability]`，并把插件配置为
`{"metrics_backend": "prometheus"}`；缺少依赖会在启动时明确失败。

## 测试

默认测试只验证 adapter boundary，不要求真实 Stripe、S3 或 OpenAI 凭据，也不会依赖
真实网络。真实外部 provider 的集成测试放在 `tests/integration/`，是 opt-in live
tests；缺少对应环境变量时会自动 `skip`。

```bash
pytest tests/integration
```

发布认证时使用更严格的门禁：

```bash
pip install -e ".[dev,live-providers]"
fastapi-infra release-check --settings infra.toml
fastapi-infra config-check --settings infra.toml
fastapi-infra config-check --settings infra.production.example.toml --env-file .env
fastapi-infra certify-providers --settings infra.production.example.toml --settings-env-file .env --env-template > provider-env-template.env
fastapi-infra certify-providers --settings infra.production.example.toml --settings-env-file .env --env-file provider.env --preflight --json > provider-preflight.json
fastapi-infra certify-providers --settings infra.production.example.toml --settings-env-file .env --env-file provider.env --json > provider-certification.json
fastapi-infra release-check --settings infra.toml \
  --provider-certification-report provider-certification.json
```

`release-check` 在不启动应用、不访问外部服务的情况下检查生产配置硬约束：生产环境
不能把 AI、支付、语音继续指向 mock provider，不能把 storage 指向 local，外部
provider 必须打开 `health_probe`，支付要有真实 durable store，`store_service =
"database"` 时必须启用 MySQL，cache 必须有 Redis，webhook 必须声明 durable store
和签名验证，任务队列和限流不能使用 memory backend。启用 observability 但仍使用
memory metrics 或关闭 tracing 时会给出 warning。`live-providers` extra 安装 live
certification 所需的 SDK 依赖。
`--env-template` 生成 `.env`/CI secrets 模板，`--env-file` 可加载一份本地凭据文件
供 `--preflight` 和 live certification 使用，避免把真实密钥长期放进 shell 环境。
`certify-providers --settings infra.production.example.toml` 会从生产配置自动推导
需要认证的 provider group，并展开依赖；例如 Stripe 会同时选择 `mysql` 和 `stripe`。
当生产配置本身使用 `{ "$env" = "NAME" }` 引用时，传
`--settings-env-file .env` 解析运行时配置；`--env-file provider.env` 则只用于
live provider certification 的凭据。
`--preflight` 在不访问 provider 的情况下检查必填环境变量和 required packages。
最终的 `certify-providers` 会把 skipped
live tests 视为未认证，并返回失败码。也可以只认证一个 provider group，例如
`fastapi-infra certify-providers --provider stripe`。provider group 覆盖该 provider
的完整生产声明；`stripe` 会同时要求 checkout session 创建、webhook 签名验证，以及
`PaymentService` + `SqlPaymentStore` + MySQL 持久化路径通过。
因为生产支付结果需要 durable database store，选择 `stripe` 也会自动包含 `mysql`
认证检查。
把 `provider-certification.json` 传给 `release-check --provider-certification-report`
可以把静态生产配置检查和真实 provider 认证报告合并成一个发布门禁。
`release-check` 默认会阻断缺少认证报告的外部 provider 配置；如果只想做本地静态扫描，
可以显式使用 `--static-only`。`release-check` 会要求认证报告覆盖配置中声明的所有已知真实 provider，
不只检查 `default_provider`。用于生产配置的认证报告必须包含可解析的
`generated_at`，且默认只在 24 小时内有效，避免长期复用旧的 live test 结果。
`release-check` 还会按当前内置 certification catalog 校验每个 provider 的
必跑 live test 名称和必需环境变量/包元数据，避免旧报告缺少新增检查项。
报告 summary 必须和 provider result 条目完全一致，重复或格式错误的 provider
result 会被阻断。`selected_providers` 必须唯一，并且和 provider result 名称
完全一致。生产发布门禁只接受从内置
`tests/integration/test_live_providers.py` 生成的 provider certification 报告。

当前 live tests 支持：

- MySQL round trip: `MYSQL_LIVE_HOST`、`MYSQL_LIVE_USER`、
  `MYSQL_LIVE_PASSWORD`、`MYSQL_LIVE_DB`，可选 `MYSQL_LIVE_PORT`、
  `MYSQL_LIVE_CONNECT_TIMEOUT`。
- Redis cache round trip: `REDIS_LIVE_URL`，可选
  `REDIS_LIVE_CONNECT_TIMEOUT`。
- Stripe checkout: `STRIPE_API_KEY`，可选 `STRIPE_API_BASE`、
  `STRIPE_LIVE_TIMEOUT`。
- Stripe webhook signature: `STRIPE_WEBHOOK_SECRET`。
- S3 put/get/list/presign: `S3_LIVE_BUCKET`、`S3_LIVE_REGION`、
  `S3_LIVE_ACCESS_KEY_ID`、`S3_LIVE_SECRET_ACCESS_KEY`，可选
  `S3_LIVE_ENDPOINT_URL`、`S3_LIVE_FORCE_PATH_STYLE`、`S3_LIVE_PREFIX`、
  `S3_LIVE_TIMEOUT`。
- OpenAI chat/embeddings: `OPENAI_API_KEY`、`OPENAI_LIVE_CHAT_MODEL`、
  `OPENAI_LIVE_EMBEDDING_MODEL`，可选 `OPENAI_API_BASE`、
  `OPENAI_LIVE_TIMEOUT`。
- Anthropic chat: `ANTHROPIC_API_KEY`、`ANTHROPIC_LIVE_CHAT_MODEL`，可选
  `ANTHROPIC_API_BASE`、`ANTHROPIC_LIVE_TIMEOUT`。
- Gemini chat/embeddings: `GEMINI_API_KEY`、`GEMINI_LIVE_CHAT_MODEL`、
  `GEMINI_LIVE_EMBEDDING_MODEL`，可选 `GEMINI_API_BASE`、
  `GEMINI_LIVE_TIMEOUT`。
- OpenAI speech ASR/TTS: `OPENAI_API_KEY`，可选 `OPENAI_API_BASE`、
  `OPENAI_ASR_MODEL`、`OPENAI_TTS_MODEL`、`OPENAI_VOICE`、
  `OPENAI_SPEECH_TIMEOUT`。
- SMTP email send: `SMTP_LIVE_HOST`、`SMTP_LIVE_SENDER`、
  `SMTP_LIVE_RECIPIENT`，可选 `SMTP_LIVE_PORT`、`SMTP_LIVE_USERNAME`、
  `SMTP_LIVE_PASSWORD`、`SMTP_LIVE_USE_TLS`、`SMTP_LIVE_TIMEOUT`。

## 文档和示例

- [架构](docs/architecture.md)
- [插件系统](docs/plugins.md)
- [Provider certification](docs/provider-certification.md)
- [AI 插件](docs/ai.md)
- [最小示例](examples/minimal/app.py)
- [AI 示例](examples/ai_app/app.py)
- [全栈插件示例](examples/full_stack/app.py)
- [外部插件包示例](examples/search_plugin/README.md)
