"""
Redis Streams 统一管理器
提供基于 Redis Streams 的消息队列功能
"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from infra.database.manager import DatabaseManager, _load_redis
from infra.logging import get_logger

logger = get_logger(__name__)


class MessageStatus(str, Enum):
    """消息状态"""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"


@dataclass
class StreamMessage:
    """Stream 消息"""

    message_id: str
    task_id: str
    task_type: str
    params: dict[str, Any]
    priority: int = 5  # 1-10，数字越大优先级越高
    retry_count: int = 0
    max_retries: int = 3
    created_at: str | None = None
    status: MessageStatus = MessageStatus.PENDING

    def __post_init__(self) -> None:
        if self.created_at is None:
            self.created_at = datetime.utcnow().isoformat()

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        data = asdict(self)
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, message_id: str, data: dict[str, Any]) -> "StreamMessage":
        """从字典创建消息"""
        # 处理 bytes 类型
        processed_data = {}
        for key, value in data.items():
            if isinstance(key, bytes):
                key = key.decode("utf-8")
            if isinstance(value, bytes):
                value = value.decode("utf-8")
            processed_data[key] = value

        # 解析 JSON 字段
        if "params" in processed_data and isinstance(processed_data["params"], str):
            processed_data["params"] = json.loads(processed_data["params"])

        processed_data["message_id"] = message_id
        processed_data["status"] = MessageStatus(processed_data.get("status", "pending"))

        return cls(**processed_data)


@dataclass
class StreamConfig:
    """Stream 配置"""

    stream_name: str
    consumer_group: str
    consumer_name: str = "default_consumer"
    max_len: int = 10000  # Stream 最大长度
    block_ms: int = 5000  # 阻塞读取超时（毫秒）
    count: int = 10  # 每次读取消息数
    max_retries: int = 3
    ack_timeout: int = 300  # ACK 超时时间（秒）
    dead_letter_suffix: str = "_dlq"

    @property
    def dead_letter_stream(self) -> str:
        """死信队列名称"""
        return f"{self.stream_name}{self.dead_letter_suffix}"


class StreamsManager:
    """
    Redis Streams 统一管理器

    特性：
    - 消费者组支持
    - 自动 ACK
    - 失败重试
    - 死信队列
    - 优先级队列（通过多 Stream 实现）
    - Pending 消息恢复
    """

    def __init__(self, config: StreamConfig, db_manager: DatabaseManager):
        """
        初始化 Streams 管理器

        Args:
            config: Stream 配置
        """
        self.config = config
        self._redis: Any | None = None
        self._initialized = False
        self._running = False
        self._consumer_task: asyncio.Task | None = None
        self._db_manager = db_manager

    async def _get_redis_client(self):
        """
        获取当前事件循环的Redis客户端

        避免在多事件循环环境下出现"Future attached to a different loop"错误
        """
        await self._db_manager.initialize()
        return await self._db_manager.get_redis_client()

    async def initialize(self):
        """初始化 Streams"""
        if self._initialized:
            return

        try:
            # 获取当前事件循环的 Redis 客户端
            self._redis = await self._get_redis_client()

            # 创建消费者组
            await self._create_consumer_group(self.config.stream_name)
            await self._create_consumer_group(self.config.dead_letter_stream)

            self._initialized = True
            logger.info(f"StreamsManager 初始化完成: {self.config.stream_name}")

        except Exception as e:
            logger.error(f"StreamsManager 初始化失败: {e}", exc_info=True)
            raise

    async def _create_consumer_group(self, stream_name: str):
        """创建消费者组"""
        try:
            redis_client = await self._get_redis_client()
            await redis_client.xgroup_create(
                name=stream_name,
                groupname=self.config.consumer_group,
                id="0",
                mkstream=True,
            )
            logger.info(f"创建消费者组: {self.config.consumer_group} on {stream_name}")
        except _load_redis().ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise
            logger.debug(f"消费者组已存在: {self.config.consumer_group} on {stream_name}")

    # ==================== 生产者方法 ====================

    async def publish(
        self,
        task_id: str,
        task_type: str,
        params: dict[str, Any],
        priority: int = 5,
        max_retries: int | None = None,
    ) -> str:
        """
        发布消息到 Stream

        Args:
            task_id: 任务ID
            task_type: 任务类型
            params: 任务参数
            priority: 优先级 (1-10)
            max_retries: 最大重试次数

        Returns:
            消息ID
        """
        if not self._initialized:
            await self.initialize()

        try:
            # 获取Redis客户端
            redis_client = await self._get_redis_client()

            message = StreamMessage(
                message_id="",  # 由 Redis 生成
                task_id=task_id,
                task_type=task_type,
                params=params,
                priority=priority,
                max_retries=max_retries or self.config.max_retries,
            )

            # 准备消息数据
            message_data = {
                "task_id": message.task_id,
                "task_type": message.task_type,
                "params": json.dumps(message.params, ensure_ascii=False),
                "priority": str(message.priority),
                "retry_count": "0",
                "max_retries": str(message.max_retries),
                "created_at": message.created_at,
                "status": MessageStatus.PENDING.value,
            }

            # 发布到 Stream (使用 MAXLEN 控制大小)
            raw_message_id = await redis_client.xadd(
                name=self.config.stream_name,
                fields=message_data,
                maxlen=self.config.max_len,
                approximate=True,
            )
            message_id = (
                raw_message_id.decode()
                if isinstance(raw_message_id, bytes)
                else str(raw_message_id)
            )

            logger.info(f"消息已发布: {message_id}, task_id={task_id}, type={task_type}")
            return message_id

        except Exception as e:
            logger.error(f"发布消息失败: {e}", exc_info=True)
            raise

    # ==================== 消费者方法 ====================

    async def start_consumer(self, handler: Callable[[StreamMessage], Awaitable[bool]]):
        """
        启动消费者

        Args:
            handler: 消息处理函数，返回 True 表示成功，False 表示失败
        """
        if self._running:
            logger.warning("消费者已在运行")
            return

        self._running = True
        self._consumer_task = asyncio.create_task(self._consume_loop(handler))
        logger.info(f"消费者已启动: {self.config.consumer_name}")

    async def stop_consumer(self):
        """停止消费者"""
        if not self._running:
            return

        self._running = False
        if self._consumer_task:
            self._consumer_task.cancel()
            try:
                await self._consumer_task
            except asyncio.CancelledError:
                pass

        logger.info(f"消费者已停止: {self.config.consumer_name}")

    async def _consume_loop(self, handler: Callable[[StreamMessage], Awaitable[bool]]):
        """消费循环"""
        while self._running:
            try:
                # 获取Redis客户端
                redis_client = await self._get_redis_client()

                # 1. 先处理 Pending 消息（恢复崩溃的任务）
                await self._claim_pending_messages(handler)

                # 2. 读取新消息
                messages = await redis_client.xreadgroup(
                    groupname=self.config.consumer_group,
                    consumername=self.config.consumer_name,
                    streams={self.config.stream_name: ">"},
                    count=self.config.count,
                    block=self.config.block_ms,
                )

                if not messages:
                    continue

                # 3. 处理消息
                for _stream_name, stream_messages in messages:
                    for message_id, message_data in stream_messages:
                        await self._process_message(message_id.decode(), message_data, handler)

            except asyncio.CancelledError:
                logger.info("消费循环被取消")
                break
            except Exception as e:
                logger.error(f"消费循环异常: {e}", exc_info=True)
                await asyncio.sleep(1)

    async def _process_message(
        self,
        message_id: str,
        message_data: dict,
        handler: Callable[[StreamMessage], Awaitable[bool]],
    ):
        """处理单条消息"""
        try:
            # 解析消息
            message = StreamMessage.from_dict(message_id, message_data)

            logger.info(f"处理消息: {message_id}, task_id={message.task_id}")

            # 调用处理函数
            success = await handler(message)

            if success:
                # 成功：ACK 消息
                await self.ack_message(message_id)
                logger.info(f"消息处理成功: {message_id}")
            else:
                # 失败：重试或进入死信队列
                await self.nack_message(message, message_data)

        except Exception as e:
            logger.error(f"处理消息异常: {message_id}, error={e}", exc_info=True)
            # 异常也视为失败
            try:
                message = StreamMessage.from_dict(message_id, message_data)
                await self.nack_message(message, message_data)
            except Exception:
                pass

    async def _claim_pending_messages(self, handler: Callable[[StreamMessage], Awaitable[bool]]):
        """认领并处理 Pending 消息（恢复机制）"""
        try:
            # 获取Redis客户端
            redis_client = await self._get_redis_client()

            # 获取 Pending 消息
            pending = await redis_client.xpending_range(
                name=self.config.stream_name,
                groupname=self.config.consumer_group,
                min="-",
                max="+",
                count=10,
            )

            if not pending:
                return

            for p in pending:
                message_id = p["message_id"].decode()
                idle_time = p["time_since_delivered"]  # 毫秒

                # 如果消息空闲时间超过阈值，认领它
                if idle_time > self.config.ack_timeout * 1000:
                    claimed = await redis_client.xclaim(
                        name=self.config.stream_name,
                        groupname=self.config.consumer_group,
                        consumername=self.config.consumer_name,
                        min_idle_time=self.config.ack_timeout * 1000,
                        message_ids=[message_id],
                    )

                    if claimed:
                        for msg_id, msg_data in claimed:
                            logger.info(f"认领 Pending 消息: {msg_id.decode()}")
                            await self._process_message(msg_id.decode(), msg_data, handler)

        except Exception as e:
            logger.error(f"认领 Pending 消息失败: {e}", exc_info=True)

    # ==================== ACK 管理 ====================

    async def ack_message(self, message_id: str):
        """确认消息已处理"""
        try:
            redis_client = await self._get_redis_client()
            await redis_client.xack(self.config.stream_name, self.config.consumer_group, message_id)
            logger.debug(f"消息已 ACK: {message_id}")
        except Exception as e:
            logger.error(f"ACK 消息失败: {message_id}, error={e}")

    async def nack_message(self, message: StreamMessage, original_data: dict):
        """消息处理失败，重试或进入死信队列"""
        try:
            message.retry_count += 1

            if message.retry_count <= message.max_retries:
                # 重新发布消息（降低优先级）
                new_priority = max(1, message.priority - 1)

                await self.publish(
                    task_id=message.task_id,
                    task_type=message.task_type,
                    params=message.params,
                    priority=new_priority,
                    max_retries=message.max_retries,
                )

                logger.info(f"消息重试: {message.message_id}, retry={message.retry_count}")
            else:
                # 进入死信队列
                await self._move_to_dead_letter(message, original_data)
                logger.warning(f"消息进入死信队列: {message.message_id}")

            # ACK 原消息
            await self.ack_message(message.message_id)

        except Exception as e:
            logger.error(f"NACK 消息失败: {message.message_id}, error={e}")

    async def _move_to_dead_letter(self, message: StreamMessage, original_data: dict):
        """移动消息到死信队列"""
        try:
            redis_client = await self._get_redis_client()

            dlq_data = original_data.copy()
            dlq_data["status"] = MessageStatus.DEAD_LETTER.value
            dlq_data["failed_at"] = datetime.utcnow().isoformat()
            dlq_data["original_message_id"] = message.message_id

            await redis_client.xadd(
                name=self.config.dead_letter_stream,
                fields=dlq_data,
                maxlen=self.config.max_len,
                approximate=True,
            )

        except Exception as e:
            logger.error(f"移动到死信队列失败: {e}")

    # ==================== 监控和管理 ====================

    async def get_stats(self) -> dict[str, Any]:
        """获取队列统计信息"""
        try:
            redis_client = await self._get_redis_client()

            # Stream 长度
            stream_len = await redis_client.xlen(self.config.stream_name)
            dlq_len = await redis_client.xlen(self.config.dead_letter_stream)

            # Pending 消息数
            pending_info = await redis_client.xpending(
                self.config.stream_name, self.config.consumer_group
            )

            stats = {
                "stream_name": self.config.stream_name,
                "stream_length": stream_len,
                "dead_letter_length": dlq_len,
                "pending_count": pending_info["pending"] if pending_info else 0,
                "consumer_group": self.config.consumer_group,
                "consumer_name": self.config.consumer_name,
                "is_running": self._running,
            }

            return stats

        except Exception as e:
            logger.error(f"获取统计信息失败: {e}")
            return {}

    async def trim_stream(self, max_len: int | None = None) -> None:
        """修剪 Stream（控制内存）"""
        try:
            redis_client = await self._get_redis_client()
            max_len = max_len or self.config.max_len
            await redis_client.xtrim(self.config.stream_name, maxlen=max_len, approximate=True)
            logger.info(f"Stream 已修剪: {self.config.stream_name}, maxlen={max_len}")
        except Exception as e:
            logger.error(f"修剪 Stream 失败: {e}")

    async def clear_stream(self):
        """清空 Stream"""
        try:
            redis_client = await self._get_redis_client()
            await redis_client.delete(self.config.stream_name)
            await redis_client.delete(self.config.dead_letter_stream)
            logger.info(f"Stream 已清空: {self.config.stream_name}")

            # 重新创建消费者组
            await self._create_consumer_group(self.config.stream_name)
            await self._create_consumer_group(self.config.dead_letter_stream)

        except Exception as e:
            logger.error(f"清空 Stream 失败: {e}")
