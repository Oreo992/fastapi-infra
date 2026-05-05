# Changelog

所有重要变更都会记录在此文件中。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

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

## [Unreleased]

### Planned
- 完善插件系统
- 添加更多示例项目
- 完善文档
- 性能优化
- 测试覆盖

---

**注**: 版本格式为 [版本号] - 日期 (YYYY-MM-DD)
