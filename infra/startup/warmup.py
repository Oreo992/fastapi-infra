"""Embedding/Milvus/音乐向量预热"""

from app.core.configuration.config import settings
from app.core.logging_pkg.logger import get_logger

logger = get_logger(__name__)


async def warmup_connections():
    """预热 Embedding、Milvus 和音乐向量连接"""
    if settings.warmup_doubao_embedding:
        try:
            import time

            from app.clients.doubao_embedding_client import get_doubao_embedding_client

            logger.info("开始预热豆包 Embedding 连接...")
            warmup_start = time.time()
            doubao_client = get_doubao_embedding_client()
            await doubao_client.create_embedding("系统启动预热")
            warmup_time = (time.time() - warmup_start) * 1000
            logger.info(f"豆包 Embedding 连接预热完成 (耗时: {warmup_time:.2f}ms)")
        except Exception as e:
            logger.warning(f"豆包 Embedding 连接预热失败: {e}")

    if settings.warmup_milvus:
        try:
            import random
            import time

            from app.core.vector.vector_store import VectorStore

            logger.info("开始预热 Milvus 向量数据库连接...")
            warmup_start = time.time()
            vector_store = VectorStore.create_store(
                store_type="milvus",
                uri=settings.milvus_uri_tools or settings.milvus_uri,
                token=settings.milvus_token_tools or settings.milvus_token,
                secure=True,
            )
            test_vector = [
                random.random()
                for _ in range(settings.doubao_embedding_target_dimension)
            ]
            try:
                await vector_store.search(
                    collection_name="tool_embeddings",
                    query_vector=test_vector,
                    top_k=1,
                    filters={"enabled": True},
                )
                warmup_time = (time.time() - warmup_start) * 1000
                logger.info(f"Milvus 连接预热完成 (耗时: {warmup_time:.2f}ms)")
            except Exception:
                warmup_time = (time.time() - warmup_start) * 1000
                logger.info(f"Milvus 连接预热完成 (耗时: {warmup_time:.2f}ms, 搜索返回空结果)")
        except Exception as e:
            logger.warning(f"Milvus 连接预热失败: {e}")

    # 预加载歌曲缓存
    try:
        from app.services.platform.music.song_search_service import SongCache

        song_cache = SongCache.get_instance()
        song_count = await song_cache.load()
        logger.info(f"歌曲缓存预加载完成: {song_count} 首")
    except Exception as e:
        logger.warning(f"歌曲缓存预加载失败（不影响主流程）: {e}")

    # 预热音乐向量服务
    try:
        import time

        from app.services.platform.music.music_vector_service import MusicVectorService

        vector_service = MusicVectorService.get_instance()
        await vector_service.ensure_collection()
        warmup_start = time.time()
        await vector_service.search_songs("预热测试", limit=1)
        warmup_time = (time.time() - warmup_start) * 1000
        logger.info(f"音乐向量服务预热完成 (首次查询耗时: {warmup_time:.0f}ms)")
    except Exception as e:
        logger.warning(f"音乐向量服务预热失败（不影响主流程）: {e}")
