"""
统一 Repository 基类

提供标准化的数据访问层抽象，支持：
- 统一的 CRUD 操作
- 弹性机制（超时/重试）
- 分页查询
- 软删除
- JSON 字段处理
- 统一的异常处理
"""

from __future__ import annotations

import json
import uuid
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from typing import Any, Generic, TypeVar

import aiomysql

from infra.exceptions import AppException
from infra.database.manager import db_manager
from infra.http.resilience import PresetConfigs, with_resilience
from infra.logging import get_logger
from infra.utils.timezone import get_timestamp


logger = get_logger(__name__)

# 泛型类型
T = TypeVar("T")


class IRepository(ABC, Generic[T]):
    """
    Repository 接口定义

    所有 Repository 必须实现此接口，确保统一的数据访问模式
    """

    @abstractmethod
    async def get_by_id(self, id: str) -> dict[str, Any] | None:
        """根据 ID 获取单个实体"""
        pass

    @abstractmethod
    async def create(self, data: dict[str, Any]) -> dict[str, Any]:
        """创建实体"""
        pass

    @abstractmethod
    async def update(self, id: str, data: dict[str, Any]) -> dict[str, Any] | None:
        """更新实体"""
        pass

    @abstractmethod
    async def delete(self, id: str) -> bool:
        """删除实体"""
        pass


class BaseRepository(IRepository[T]):
    """
    Repository 基类

    提供标准化的数据访问实现，子类可以：
    1. 直接使用基类方法
    2. 重写方法以实现自定义逻辑
    3. 扩展新方法

    Features:
    - 自动添加弹性机制（超时/重试）
    - 统一的异常处理
    - 软删除支持（可配置）
    - 分页查询支持
    - JSON 字段自动处理

    Usage:
        class AgentRepository(BaseRepository):
            def __init__(self):
                super().__init__(
                    table_name="agents",
                    soft_delete=True,
                    soft_delete_field="status",
                    soft_delete_value="offline"
                )
    """

    def __init__(
        self,
        table_name: str,
        *,
        soft_delete: bool = True,
        soft_delete_field: str = "is_active",
        soft_delete_value: Any = 0,
        active_value: Any = 1,
        id_field: str = "id",
        created_at_field: str = "created_at",
        updated_at_field: str = "updated_at",
    ):
        """
        初始化 Repository

        Args:
            table_name: 数据库表名
            soft_delete: 是否启用软删除
            soft_delete_field: 软删除字段名
            soft_delete_value: 软删除时的字段值
            active_value: 活跃状态的字段值
            id_field: 主键字段名
            created_at_field: 创建时间字段名
            updated_at_field: 更新时间字段名
        """
        self.table_name = table_name
        self.soft_delete = soft_delete
        self.soft_delete_field = soft_delete_field
        self.soft_delete_value = soft_delete_value
        self.active_value = active_value
        self.id_field = id_field
        self.created_at_field = created_at_field
        self.updated_at_field = updated_at_field

        # 数据库管理器
        self._db = db_manager

    # ==================== 连接管理 ====================

    @asynccontextmanager
    async def get_connection(self):
        """获取数据库连接（上下文管理器）"""
        await self._db.initialize()
        async with self._db.get_connection() as conn:
            yield conn

    # ==================== 基础 CRUD 操作 ====================

    @with_resilience(
        timeout_config=PresetConfigs.DB_TIMEOUT, retry_config=PresetConfigs.DB_RETRY
    )
    async def get_by_id(self, id: str) -> dict[str, Any] | None:
        """
        根据 ID 获取单个实体

        Args:
            id: 实体 ID

        Returns:
            实体数据字典，不存在返回 None
        """
        where_clause = f"{self.id_field} = %s"
        if self.soft_delete:
            where_clause += f" AND {self.soft_delete_field} = %s"
            params = (id, self.active_value)
        else:
            params = (id,)

        async with self.get_connection() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                sql = f"SELECT * FROM {self.table_name} WHERE {where_clause}"
                await cursor.execute(sql, params)
                result = await cursor.fetchone()

                if result:
                    return self._process_row(dict(result))
                return None

    @with_resilience(
        timeout_config=PresetConfigs.DB_TIMEOUT, retry_config=PresetConfigs.DB_RETRY
    )
    async def create(self, data: dict[str, Any]) -> dict[str, Any]:
        """
        创建实体

        Args:
            data: 实体数据字典

        Returns:
            创建后的实体数据（包含自动生成的字段）

        Raises:
            AppException: 创建失败时抛出
        """
        try:
            # 准备数据
            insert_data = self._prepare_insert_data(data)

            # 构建 SQL
            columns = list(insert_data.keys())
            placeholders = ", ".join(["%s"] * len(columns))
            column_names = ", ".join(columns)
            values = [insert_data[col] for col in columns]

            async with self.get_connection() as conn:
                async with conn.cursor() as cursor:
                    sql = f"INSERT INTO {self.table_name} ({column_names}) VALUES ({placeholders})"
                    await cursor.execute(sql, values)
                    await conn.commit()

            logger.info(f"创建 {self.table_name}: {insert_data.get(self.id_field)}")
            return insert_data

        except Exception as e:
            logger.error(f"创建 {self.table_name} 失败: {e}")
            raise self._repository_error("create", e) from e

    @with_resilience(
        timeout_config=PresetConfigs.DB_TIMEOUT, retry_config=PresetConfigs.DB_RETRY
    )
    async def update(self, id: str, data: dict[str, Any]) -> dict[str, Any] | None:
        """
        更新实体

        Args:
            id: 实体 ID
            data: 要更新的字段

        Returns:
            更新后的实体数据，实体不存在返回 None

        Raises:
            AppException: 更新失败时抛出
        """
        if not data:
            return await self.get_by_id(id)

        try:
            # 添加更新时间
            update_data = {**data, self.updated_at_field: get_timestamp()}

            # 构建 SET 子句
            set_clauses = [f"{col} = %s" for col in update_data]
            params = list(update_data.values())

            # 构建 WHERE 子句
            where_clause = f"{self.id_field} = %s"
            params.append(id)

            if self.soft_delete:
                where_clause += f" AND {self.soft_delete_field} = %s"
                params.append(self.active_value)

            async with self.get_connection() as conn:
                async with conn.cursor() as cursor:
                    sql = f"UPDATE {self.table_name} SET {', '.join(set_clauses)} WHERE {where_clause}"
                    await cursor.execute(sql, params)
                    await conn.commit()

                    if cursor.rowcount > 0:
                        logger.info(f"更新 {self.table_name}: {id}")
                        return await self.get_by_id(id)
                    return None

        except Exception as e:
            logger.error(f"更新 {self.table_name} 失败: {e}")
            raise self._repository_error("update", e) from e

    @with_resilience(
        timeout_config=PresetConfigs.DB_TIMEOUT, retry_config=PresetConfigs.DB_RETRY
    )
    async def delete(self, id: str) -> bool:
        """
        删除实体（软删除或硬删除，取决于配置）

        Args:
            id: 实体 ID

        Returns:
            是否删除成功
        """
        try:
            async with self.get_connection() as conn:
                async with conn.cursor() as cursor:
                    if self.soft_delete:
                        # 软删除
                        sql = f"""
                            UPDATE {self.table_name}
                            SET {self.soft_delete_field} = %s, {self.updated_at_field} = %s
                            WHERE {self.id_field} = %s
                        """
                        await cursor.execute(
                            sql, (self.soft_delete_value, get_timestamp(), id)
                        )
                    else:
                        # 硬删除
                        sql = (
                            f"DELETE FROM {self.table_name} WHERE {self.id_field} = %s"
                        )
                        await cursor.execute(sql, (id,))

                    await conn.commit()

                    if cursor.rowcount > 0:
                        logger.info(f"删除 {self.table_name}: {id}")
                        return True
                    return False

        except Exception as e:
            logger.error(f"删除 {self.table_name} 失败: {e}")
            raise self._repository_error("delete", e) from e

    def _repository_error(self, operation: str, error: Exception) -> AppException:
        return AppException(
            message=f"Repository {operation} failed for {self.table_name}: {error}",
            error_code="EXTERNAL_SERVICE_ERROR",
            details={
                "service_name": "database",
                "operation": operation,
                "table_name": self.table_name,
            },
        )

    # ==================== 查询方法 ====================

    @with_resilience(
        timeout_config=PresetConfigs.DB_TIMEOUT, retry_config=PresetConfigs.DB_RETRY
    )
    async def get_all(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        order_by: str | None = None,
        order_desc: bool = True,
    ) -> tuple[list[dict[str, Any]], int]:
        """
        获取所有实体（分页）

        Args:
            page: 页码（从 1 开始）
            page_size: 每页大小
            order_by: 排序字段
            order_desc: 是否降序

        Returns:
            (实体列表, 总数) 元组
        """
        offset = (page - 1) * page_size
        order_by = order_by or self.created_at_field
        order_direction = "DESC" if order_desc else "ASC"

        # 构建 WHERE 子句
        where_clause = ""
        params = []
        if self.soft_delete:
            where_clause = f"WHERE {self.soft_delete_field} = %s"
            params.append(self.active_value)

        async with self.get_connection() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                # 查询总数
                count_sql = (
                    f"SELECT COUNT(*) as total FROM {self.table_name} {where_clause}"
                )
                await cursor.execute(count_sql, params if params else None)
                count_result = await cursor.fetchone()
                total = count_result["total"] if count_result else 0

                # 查询数据
                sql = f"""
                    SELECT * FROM {self.table_name}
                    {where_clause}
                    ORDER BY {order_by} {order_direction}
                    LIMIT %s OFFSET %s
                """
                await cursor.execute(sql, (*params, page_size, offset))
                rows = await cursor.fetchall()

                items = [self._process_row(dict(row)) for row in rows] if rows else []
                return items, total

    @with_resilience(
        timeout_config=PresetConfigs.DB_TIMEOUT, retry_config=PresetConfigs.DB_RETRY
    )
    async def find_by(
        self,
        conditions: dict[str, Any],
        *,
        page: int = 1,
        page_size: int = 20,
        order_by: str | None = None,
        order_desc: bool = True,
    ) -> tuple[list[dict[str, Any]], int]:
        """
        根据条件查询实体

        Args:
            conditions: 查询条件字典 {字段名: 值}
            page: 页码
            page_size: 每页大小
            order_by: 排序字段
            order_desc: 是否降序

        Returns:
            (实体列表, 总数) 元组
        """
        offset = (page - 1) * page_size
        order_by = order_by or self.created_at_field
        order_direction = "DESC" if order_desc else "ASC"

        # 构建 WHERE 子句
        where_parts = []
        params = []

        for field, value in conditions.items():
            if value is None:
                where_parts.append(f"{field} IS NULL")
            else:
                where_parts.append(f"{field} = %s")
                params.append(value)

        if self.soft_delete:
            where_parts.append(f"{self.soft_delete_field} = %s")
            params.append(self.active_value)

        where_clause = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

        async with self.get_connection() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                # 查询总数
                count_sql = (
                    f"SELECT COUNT(*) as total FROM {self.table_name} {where_clause}"
                )
                await cursor.execute(count_sql, params if params else None)
                count_result = await cursor.fetchone()
                total = count_result["total"] if count_result else 0

                # 查询数据
                sql = f"""
                    SELECT * FROM {self.table_name}
                    {where_clause}
                    ORDER BY {order_by} {order_direction}
                    LIMIT %s OFFSET %s
                """
                await cursor.execute(sql, (*params, page_size, offset))
                rows = await cursor.fetchall()

                items = [self._process_row(dict(row)) for row in rows] if rows else []
                return items, total

    @with_resilience(
        timeout_config=PresetConfigs.DB_TIMEOUT, retry_config=PresetConfigs.DB_RETRY
    )
    async def find_one_by(self, conditions: dict[str, Any]) -> dict[str, Any] | None:
        """
        根据条件查询单个实体

        Args:
            conditions: 查询条件字典

        Returns:
            实体数据，不存在返回 None
        """
        items, _ = await self.find_by(conditions, page=1, page_size=1)
        return items[0] if items else None

    @with_resilience(
        timeout_config=PresetConfigs.DB_TIMEOUT, retry_config=PresetConfigs.DB_RETRY
    )
    async def count(self, conditions: dict[str, Any] | None = None) -> int:
        """
        统计数量

        Args:
            conditions: 可选的查询条件

        Returns:
            实体数量
        """
        where_parts = []
        params = []

        if conditions:
            for field, value in conditions.items():
                if value is None:
                    where_parts.append(f"{field} IS NULL")
                else:
                    where_parts.append(f"{field} = %s")
                    params.append(value)

        if self.soft_delete:
            where_parts.append(f"{self.soft_delete_field} = %s")
            params.append(self.active_value)

        where_clause = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

        async with self.get_connection() as conn, conn.cursor() as cursor:
            sql = f"SELECT COUNT(*) FROM {self.table_name} {where_clause}"
            await cursor.execute(sql, params if params else None)
            result = await cursor.fetchone()
            return result[0] if result else 0

    @with_resilience(
        timeout_config=PresetConfigs.DB_TIMEOUT, retry_config=PresetConfigs.DB_RETRY
    )
    async def exists(self, id: str) -> bool:
        """
        检查实体是否存在

        Args:
            id: 实体 ID

        Returns:
            是否存在
        """
        return await self.get_by_id(id) is not None

    @with_resilience(
        timeout_config=PresetConfigs.DB_TIMEOUT, retry_config=PresetConfigs.DB_RETRY
    )
    async def get_by_ids(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        """
        批量获取实体（解决 N+1 查询问题）

        Args:
            ids: ID 列表

        Returns:
            {id: entity} 字典映射
        """
        if not ids:
            return {}

        placeholders = ",".join(["%s"] * len(ids))
        where_clause = f"{self.id_field} IN ({placeholders})"
        params = list(ids)

        if self.soft_delete:
            where_clause += f" AND {self.soft_delete_field} = %s"
            params.append(self.active_value)

        async with self.get_connection() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                sql = f"SELECT * FROM {self.table_name} WHERE {where_clause}"
                await cursor.execute(sql, params)
                rows = await cursor.fetchall()

                result = {}
                for row in rows or []:
                    entity = self._process_row(dict(row))
                    result[entity[self.id_field]] = entity
                return result

    # ==================== 辅助方法 ====================

    def _prepare_insert_data(self, data: dict[str, Any]) -> dict[str, Any]:
        """
        准备插入数据

        自动添加 ID、时间戳等字段
        """
        insert_data = {**data}

        # 自动生成 ID
        if self.id_field not in insert_data or not insert_data[self.id_field]:
            insert_data[self.id_field] = str(uuid.uuid4())

        # 添加时间戳
        now = get_timestamp()
        if self.created_at_field not in insert_data:
            insert_data[self.created_at_field] = now
        if self.updated_at_field not in insert_data:
            insert_data[self.updated_at_field] = now

        # 设置活跃状态
        if self.soft_delete and self.soft_delete_field not in insert_data:
            insert_data[self.soft_delete_field] = self.active_value

        return insert_data

    def _process_row(self, row: dict[str, Any]) -> dict[str, Any]:
        """
        处理查询结果行

        子类可以重写此方法以添加自定义处理逻辑（如 JSON 字段解析）
        """
        return row

    # ==================== JSON 字段处理工具 ====================

    @staticmethod
    def safe_json_parse(value: Any, default: Any = None) -> Any:
        """
        安全地解析 JSON 字符串

        Args:
            value: 要解析的值
            default: 解析失败时的默认值

        Returns:
            解析后的对象或默认值
        """
        if not value:
            return default
        if not isinstance(value, str):
            return value

        value = value.strip()
        if not value:
            return default

        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            logger.warning(f"JSON 解析失败: {value[:100]}...")
            return default

    @staticmethod
    def safe_json_dumps(value: Any) -> str | None:
        """
        安全地序列化为 JSON 字符串

        Args:
            value: 要序列化的值

        Returns:
            JSON 字符串或 None
        """
        if value is None:
            return None

        if isinstance(value, str):
            value = value.strip()
            if not value:
                return None
            # 验证是否已经是有效 JSON
            try:
                json.loads(value)
                return value
            except (json.JSONDecodeError, ValueError):
                return json.dumps(value, ensure_ascii=False)

        return json.dumps(value, ensure_ascii=False)


class UnitOfWork:
    """
    工作单元模式

    支持跨多个 Repository 的事务操作

    Usage:
        async with UnitOfWork() as uow:
            # 在同一事务中执行多个操作
            await agent_repo.create(data, connection=uow.connection)
            await tool_repo.update(id, data, connection=uow.connection)
            # 自动提交或回滚
    """

    def __init__(self):
        self._connection = None
        self._in_transaction = False

    async def __aenter__(self):
        await db_manager.initialize()
        self._connection = await db_manager.acquire_connection()
        await self._connection.begin()
        self._in_transaction = True
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        try:
            if exc_type is None and self._in_transaction:
                await self._connection.commit()
            else:
                await self._connection.rollback()
        finally:
            if self._connection:
                await db_manager.release_connection(self._connection)
                self._connection = None
                self._in_transaction = False

    async def commit(self):
        """手动提交事务"""
        if self._in_transaction:
            await self._connection.commit()
            self._in_transaction = False

    async def rollback(self):
        """手动回滚事务"""
        if self._in_transaction:
            await self._connection.rollback()
            self._in_transaction = False

    @property
    def connection(self):
        """获取当前事务连接"""
        return self._connection
