"""
时区帮助工具
提供统一的时区处理函数
"""

from datetime import UTC, datetime, timedelta, timezone


def get_timestamp() -> datetime:
    """获取上海时区的当前时间（东八区，UTC+8）"""
    shanghai_tz = timezone(timedelta(hours=8))
    return datetime.now(shanghai_tz)


def get_beijing_time() -> datetime:
    """获取北京时区的当前时间（东八区，UTC+8）"""
    return get_timestamp()


def utc_to_shanghai(utc_time: datetime) -> datetime:
    """将UTC时间转换为上海时区时间"""
    if utc_time.tzinfo is None:
        # 如果没有时区信息，假设为UTC
        utc_time = utc_time.replace(tzinfo=UTC)

    shanghai_tz = timezone(timedelta(hours=8))
    return utc_time.astimezone(shanghai_tz)


def shanghai_to_utc(shanghai_time: datetime) -> datetime:
    """将上海时区时间转换为UTC时间"""
    if shanghai_time.tzinfo is None:
        # 如果没有时区信息，假设为上海时区
        shanghai_tz = timezone(timedelta(hours=8))
        shanghai_time = shanghai_time.replace(tzinfo=shanghai_tz)

    return shanghai_time.astimezone(UTC)
