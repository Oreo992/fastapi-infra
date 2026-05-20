# Changelog

所有重要变更都会记录在此文件中。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [0.2.0] - 2026-05-12

### Breaking

- Replaced the legacy startup/registry/container helpers with the small public
  API: `InfraSettings`, `PluginSettings`, `InfraContext`, and `setup_infra`.
- Removed compatibility modules `infra.registry`, `infra.startup`, and
  `infra.concurrency`.
- Built-in plugins now default to disabled. Applications must enable required
  capabilities through `InfraSettings.infra.plugins`.
- Database, cache, HTTP, logging, and provider services no longer expose
  process-wide singleton helper APIs. Callers pass explicit manager/service
  instances through the plugin context.
- API keys are configured as hashes only; plaintext API key configuration is not
  supported.

### Added

- Plugin platform with dependency ordering, rollback on startup failure,
  retryable shutdown state, strict plugin config validation, and aggregated
  health.
- Optional provider plugins for AI, speech, auth, database, cache, HTTP,
  observability, tasks, storage, webhooks, payment, rate limiting, and
  notifications.
- Official SDK adapter boundaries for OpenAI, Anthropic, and Gemini AI
  providers, including OpenAI and Gemini embeddings.
- Real provider adapters for Stripe checkout/refunds/webhook signatures,
  S3-compatible object storage, OpenAI ASR/TTS, SMTP notifications, Redis cache,
  Redis Streams tasks, and MySQL.
- Provider health probing that keeps external providers `degraded` unless a real
  upstream probe is explicitly enabled.
- Live provider certification CLI and CI workflow with JSON reports, preflight
  checks, environment templates, and skipped checks counted as not certified.
- Project scaffolding and migration commands through the `fastapi-infra` CLI.
- Prometheus metrics and OpenTelemetry span integration behind optional
  observability configuration.
- External plugin discovery through the `fastapi_infra.plugins` Python entry
  point group.
- External AI, payment, and speech provider discovery through narrow provider
  entry point groups.

### Changed

- Health responses redact secret-looking values in messages and details.
- Payment and webhook stores can be backed by durable SQL tables without tying
  infrastructure records to an application order model.
- Background task workers expose retry, delayed delivery, and dead-letter
  behavior explicitly.
- Distribution packaging now verifies wheel install/import behavior without
  importing optional provider SDKs.
- Auth health now reports `degraded` when the plugin is enabled without any API
  keys or JWT signing configuration.
- Live provider CI keeps generating JSON evidence for both preflight and
  certification even when one of the checks fails.

## [0.1.0] - 2026-04-16

### Added

**核心基础设施**
- HTTP 客户端（HttpClient + 弹性机制）
- 数据库管理（DatabaseManager + BaseRepository）
- 缓存服务（CacheService）
- 日志系统（LoggerManager + 分布式追踪）
- 服务注册（ServiceRegistry）
- 依赖注入（ServiceContainer）
- 配置管理（BaseSettings + 跨平台支持）
- 统一异常体系（AppException 及其子类）
- 工具函数（时间戳等）

**插件基础**
- 分布式锁（DistributedLockManager）
- 事务协调器（TransactionCoordinator）
- 插件系统框架（待完善）

**文档和示例**
- README 使用指南
- 最小化示例项目
- 设计方案文档
- 简化启动脚本

### Changed
- 从 AI_Server 母包提取基础设施代码
- 所有配置改为注入方式（不硬编码）
- 删除业务特定逻辑，保持通用性

### Removed
- 业务特定配置（LLM、Agent 等）
- 客户端审计日志功能（业务特定）
- 业务接口导入和便捷函数

---
