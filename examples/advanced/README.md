# 高级示例

展示 fastapi-infra 的完整功能。

## 功能展示

- ✅ 生命周期管理（LifecycleManager）
- ✅ 请求日志中间件（RequestLoggingMiddleware）
- ✅ 并发控制（GlobalThreadPoolManager）
- ✅ Redis Streams 消息队列
- ✅ 缓存管理
- ✅ 统一API响应格式

## 运行

```bash
# 确保已配置 .env 文件
cp ../../.env.example .env

# 编辑 .env 填入数据库和 Redis 配置

# 启动应用
python app.py
```

## 测试

```bash
# 健康检查
curl http://localhost:8000/health

# 创建异步任务
curl -X POST http://localhost:8000/tasks \
  -H "Content-Type: application/json" \
  -d '{"task_type":"email","params":{"to":"user@example.com"}}'

# 缓存演示
curl http://localhost:8000/cache/demo
```
