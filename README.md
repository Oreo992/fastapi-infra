# FastAPI InfraKit

FastAPI InfraKit 是一套给 FastAPI 项目用的基础设施层。

它不想替你写业务代码，只把后端项目里反复要搭的东西先整理好：配置、插件、健康检查、发布检查、provider 预检、任务队列、脚手架和本地验证。

包名和命令行仍然是 `fastapi-infra`。

## 它现在能做什么

- `fastapi-infra new` 生成一个可以直接跑测试的 FastAPI 项目。
- 内置 auth、database、cache、http、observability、ai、speech、payment、storage、notifications、webhooks、tasks、ratelimit 插件。
- 插件默认关闭，需要在配置里显式启用。
- 发布前检查 mock provider、memory store、弱 secret、缺失 provider 凭据和不完整生产依赖。
- 任务队列有真实 backend：Redis Streams、SQS、Kafka、Celery。
- 支持外部插件和 provider/backend adapter。
- 本地验证脚本覆盖测试、打包、生成项目 smoke test 和插件模板 smoke test。

## 安装

在这个仓库里开发：

```bash
pip install -e ".[dev]"
```

在业务项目里只装需要的能力：

```bash
pip install "fastapi-infra[http,mysql,redis,observability]"
pip install "fastapi-infra[tasks-sqs]"
pip install "fastapi-infra[ai-openai]"
```

常用 extras：

| Extra | 作用 |
| --- | --- |
| `mysql` | MySQL 支持 |
| `redis` | Redis cache、锁、限流、Redis 任务队列 |
| `http` | AIOHTTP/HTTPX 出站 HTTP |
| `observability` | Prometheus 和 OpenTelemetry |
| `ai-openai`, `ai-anthropic`, `ai-gemini`, `ai` | AI provider SDK |
| `tasks-redis`, `tasks-sqs`, `tasks-kafka`, `tasks-celery` | 任务队列 backend |
| `live-providers` | opt-in live provider test 依赖 |

## 创建项目

```bash
fastapi-infra profiles
fastapi-infra new services/billing-api --profile saas
cd services/billing-api
pip install -e ".[dev]"
make verify
uvicorn app.main:app --reload
```

发布前跑这些：

```bash
make env
make release-static
make provider-list
make provider-preflight
```

生成的项目会包含 `app/`、测试、`infra.toml`、`infra.production.example.toml`、`.env.example`、`provider.env.example`、Docker 文件、GitHub Actions、Makefile、发布脚本和 `infra.manifest.json`。

## Profiles

| Profile | 适合什么 | 启用插件 |
| --- | --- | --- |
| `minimal` | 最小 FastAPI 项目 | 无 |
| `api` | 常规 API 服务 | auth, database, cache, http, observability, ratelimit |
| `worker` | 后台任务服务 | database, cache, http, tasks, observability |
| `ai` | AI 应用或模型网关 | ai, speech, database, cache, http, observability |
| `saas` | SaaS 后端 | auth, database, cache, http, observability, payment, storage, notifications, webhooks, ratelimit, tasks |
| `full` | 集成测试或探索 | 全部内置插件 |

可以在 profile 上继续追加插件：

```bash
fastapi-infra new services/agent-api --profile api --plugins ai,speech,tasks
```

不传 `--profile` 时默认是 `minimal`。

## 内置插件

| 插件 | 本地默认 | 生产用法 |
| --- | --- | --- |
| `auth` | 本地 JWT/API key 配置 | 强 secret、key rotation、PBKDF2 API key hash |
| `database` | memory/local helper | MySQL 和 Redis 连接 |
| `cache` | memory cache | Redis cache |
| `http` | 本地 client 配置 | AIOHTTP client、retry、timeout、trace header |
| `observability` | 进程内 health/metrics | Prometheus 和 OpenTelemetry |
| `ai` | mock provider | OpenAI、Anthropic、Gemini 或自定义 provider |
| `speech` | mock provider | OpenAI speech 或自定义 ASR/TTS |
| `payment` | mock provider | Stripe checkout、refund、webhook、durable store |
| `storage` | 本地文件系统 | S3-compatible storage |
| `notifications` | noop sender | SMTP 或 signed webhook |
| `webhooks` | memory store | signed provider 和 durable idempotency store |
| `tasks` | memory queue | Redis Streams、SQS、Kafka、Celery 或自定义 backend |
| `ratelimit` | memory counter | Redis fixed-window rate limiting |

## 配置

插件默认关闭，按需启用：

```toml
[infra.plugins.payment]
enabled = true

[infra.plugins.payment.config.providers.stripe]
api_key = { "$env" = "STRIPE_API_KEY" }
webhook_secret = { "$env" = "STRIPE_WEBHOOK_SECRET" }
```

环境变量可以覆盖嵌套配置：

```bash
INFRA__INFRA__PLUGINS__AUTH__ENABLED=true
INFRA__INFRA__PLUGINS__AUTH__CONFIG__JWT_SECRET=change-me-in-production
```

不启动应用也能检查配置：

```bash
fastapi-infra config-check --settings infra.toml
fastapi-infra project-check .
```

## 在 FastAPI 里取服务

插件会把 typed service 注册到 infrastructure context。路由里用 `infra_service`，不要自己从 `app.state` 里到处拿对象：

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

## 健康检查和指标

observability 插件可以挂载健康检查和指标路由：

```python
from infra.plugins.observability import install_observability_routes

install_observability_routes(app, infra, prefix="/ops")
```

会得到：

- `/ops/healthz`
- `/ops/readyz`
- `/ops/metrics`

## 发布检查

release checker 的作用是提前拦住常见生产事故：生产配置还在用 mock provider、memory-only store、占位 secret、缺少 provider report，或者生产依赖没配完整。

```bash
fastapi-infra config-check --settings infra.production.example.toml --env-file .env
fastapi-infra release-check --settings infra.production.example.toml --env-file .env --static-only
fastapi-infra certify-providers --settings infra.production.example.toml --settings-env-file .env --list --requirements
fastapi-infra certify-providers --settings infra.production.example.toml --settings-env-file .env --preflight --env-file provider.env
```

live provider tests 是 opt-in，只有准备好真实凭据时才跑。

当前 live coverage 包括 MySQL、Redis、Stripe、S3-compatible storage、OpenAI、Anthropic、Gemini、OpenAI Speech 和 SMTP。

## 任务队列

`tasks` 插件支持 idempotency、delay、retry、dead-letter、worker concurrency 和 metrics。

| Backend | Extra |
| --- | --- |
| Memory | 无 |
| Redis Streams | `tasks-redis` |
| SQS | `tasks-sqs` |
| Kafka | `tasks-kafka` |
| Celery | `tasks-celery` |

自定义任务队列 backend 通过 `fastapi_infra.task_queue_backends` 注册。

## 扩展

创建一个 service plugin：

```bash
fastapi-infra plugins init search providers/search-plugin
cd providers/search-plugin
pip install -e ".[dev]"
python -m pytest -q
fastapi-infra plugins check search --settings infra.example.toml --lifecycle
```

创建 provider/backend adapter：

```bash
fastapi-infra plugins init openrouter providers/openrouter --kind provider --provider-kind ai
fastapi-infra plugins init adyen providers/adyen --kind provider --provider-kind payment
fastapi-infra plugins init deepgram providers/deepgram --kind provider --provider-kind speech
fastapi-infra plugins init r2 providers/r2 --kind provider --provider-kind storage
fastapi-infra plugins init nats providers/nats --kind provider --provider-kind tasks
```

支持的 entry point groups：

- `fastapi_infra.plugins`
- `fastapi_infra.ai_providers`
- `fastapi_infra.payment_providers`
- `fastapi_infra.speech_providers`
- `fastapi_infra.storage_providers`
- `fastapi_infra.notification_providers`
- `fastapi_infra.webhook_providers`
- `fastapi_infra.task_queue_backends`
- `fastapi_infra.ratelimit_backends`
- `fastapi_infra.provider_checks`

## 开发

```bash
pip install -e ".[dev]"
python scripts/verify_local.py
```

打包和脚手架 smoke test：

```bash
python scripts/verify_local.py --skip-core --package --smoke
```

插件模板 smoke test：

```bash
python scripts/smoke_plugin_templates.py --work-dir /tmp/fastapi-infra-plugin-template-smoke
```

## 目录

```text
infra/
  core/                 App context、lifecycle、service registry、health
  config/               Settings loading and validation
  plugins/              内置插件和扩展 helper
  provider_tests/       Opt-in live provider tests
scripts/
  verify_local.py       本地验证入口
  check_distribution.py Package metadata checker
  smoke_*.py            脚手架和插件模板 smoke tests
tests/
  core/                 Core、CLI、project metadata、release checks
  plugins/              内置插件测试
```

## License

MIT.
