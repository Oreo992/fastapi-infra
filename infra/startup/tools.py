"""内置工具同步和MCP初始化"""

from fastapi import FastAPI

from app.core.logging_pkg.logger import get_logger

logger = get_logger(__name__)


async def init_builtin_tools():
    """同步内置工具到数据库"""
    try:
        from app.services.platform.tool.builtin_tool_sync_service import (
            get_builtin_tool_sync_service,
        )

        sync_service = get_builtin_tool_sync_service()
        sync_stats = await sync_service.sync_all_builtin_tools()
        logger.info(
            f"内置工具同步完成: 新建={sync_stats['created']}, "
            f"更新={sync_stats['updated']}, 跳过={sync_stats['skipped']}, "
            f"错误={sync_stats['errors']}"
        )
    except Exception as e:
        logger.warning(f"内置工具同步失败: {e}")


async def init_mcp(app: FastAPI):
    """初始化 MCP Server 和 Agent 工具管理器"""
    try:
        from app.services.platform.mcp.agent_tool_manager import get_agent_tool_manager
        from app.services.platform.mcp.unified_mcp_server import get_unified_mcp_server

        unified_server = await get_unified_mcp_server()
        logger.info("统一 MCP Server 初始化完成")

        agent_tool_mgr = await get_agent_tool_manager(unified_server)
        app.state.agent_tool_manager = agent_tool_mgr
        logger.info("Agent 工具管理器初始化完成")

        try:
            all_tools = await agent_tool_mgr._get_all_tools_from_server()
            logger.info(f"已加载 {len(all_tools)} 个工具")
        except Exception as e:
            logger.warning(f"获取工具统计失败: {e}")
    except Exception as e:
        logger.warning(f"MCP Server 初始化失败: {e}")

    try:
        from app.services.platform.mcp.mcp_client_manager import MCPClientManager

        mcp_client_manager = MCPClientManager()
        app.state.mcp_client_manager = mcp_client_manager

        sync_enabled = False
        if sync_enabled:
            logger.info("MCP在线同步已启用，开始同步工具...")
        else:
            logger.info("MCP在线同步已禁用，跳过工具同步")

        stats = await mcp_client_manager.initialize_all(sync_tools=sync_enabled)
        logger.info(
            f"MCP连接初始化完成: "
            f"总数={stats['total']}, 连接={stats['connected']}, "
            f"同步={stats['synced']}, 失败={stats['failed']}"
        )
    except Exception as e:
        logger.warning(f"MCP连接初始化失败: {e}")

    try:
        from app.services.platform.mcp.mcp_cache_scheduler import start_mcp_cache_scheduler

        await start_mcp_cache_scheduler()
        logger.info("MCP工具缓存调度器启动完成")
    except Exception as e:
        logger.warning(f"MCP工具缓存调度器启动失败: {e}")

    logger.info("Chat服务使用请求级别的依赖注入，提供更好的并发安全性")
