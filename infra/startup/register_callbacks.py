"""
回调处理器启动注册

在应用启动时注册所有工具回调处理器
"""

from app.core.logging_pkg.logger import get_logger
from app.services.platform.chat.callback_handler import get_callback_registry
from app.services.platform.chat.callbacks import (
    FortuneCallbackHandler,
    NatalChartCallbackHandler,
    PlaySoundCallbackRouter,
    TarotCardCallbackHandler,
)


logger = get_logger(__name__)


def register_all_callbacks():
    """
    注册所有回调处理器

    在应用启动时调用，注册各种工具的回调处理器到全局注册表。
    处理器注册顺序：
    1. NatalChartCallbackHandler - 处理 generate_natal_chart 回调
    2. TarotCardCallbackHandler - 处理 draw_tarot_card 回调
    3. FortuneCallbackHandler - 处理 get_zodiac_daily_fortune / get_shengxiao_daily_fortune 回调
    4. PlaySoundCallbackRouter - 处理 play_sound 回调（路由到上述处理器）
    """
    registry = get_callback_registry()

    # 创建各个处理器实例
    natal_handler = NatalChartCallbackHandler()
    tarot_handler = TarotCardCallbackHandler()
    fortune_handler = FortuneCallbackHandler()

    # 注册本命盘回调处理器
    registry.register(natal_handler)
    logger.info(
        f"回调处理器注册完成: {natal_handler.tool_names}",
        extra={"handler": type(natal_handler).__name__},
    )

    # 注册塔罗牌回调处理器
    registry.register(tarot_handler)
    logger.info(
        f"回调处理器注册完成: {tarot_handler.tool_names}",
        extra={"handler": type(tarot_handler).__name__},
    )

    # 注册运势查询回调处理器
    registry.register(fortune_handler)
    logger.info(
        f"回调处理器注册完成: {fortune_handler.tool_names}",
        extra={"handler": type(fortune_handler).__name__},
    )

    # 注册音效播放回调路由器（共享处理器实例，避免重复创建）
    sound_router = PlaySoundCallbackRouter(
        natal_handler=natal_handler,
        tarot_handler=tarot_handler,
        fortune_handler=fortune_handler,
    )
    registry.register(sound_router)
    logger.info(
        f"回调处理器注册完成: {sound_router.tool_names}",
        extra={"handler": type(sound_router).__name__},
    )

    # 列出所有已注册的工具
    registered_tools = registry.list_registered_tools()
    logger.info(
        f"已注册 {len(registered_tools)} 个工具回调处理器",
        extra={"tools": registered_tools},
    )
