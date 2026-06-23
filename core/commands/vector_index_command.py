"""
向量索引手动重建命令
"""

from typing import Any

from astrbot.api import AstrMessageEvent, logger
from astrbot.api.event.filter import PermissionType


async def rebuild_vector_index(plugin_instance: Any, event: AstrMessageEvent):
    """手动重建向量索引。"""
    try:
        vector_svc = getattr(plugin_instance, "vector_index_service", None)
        if not vector_svc or not getattr(vector_svc, "_initialized", False):
            # 尝试初始化
            emb_provider = None
            emb_config_id = getattr(plugin_instance.plugin_config, "embedding_provider_id", "") or ""
            if emb_config_id:
                try:
                    emb_provider = plugin_instance.context.get_provider_by_id(emb_config_id)
                except Exception:
                    pass
            if emb_provider is None:
                embedding_providers = plugin_instance.context.get_all_embedding_providers()
                if embedding_providers:
                    emb_provider = embedding_providers[0]

            if emb_provider is None:
                yield "❌ 没有可用的 Embedding Provider。请先在 AstrBot 中配置 embedding 模型（如 BGE-M3 等），或在插件配置中设置 `embedding_provider_id`。"
                return

            vec_db_path = str(plugin_instance.cache_dir / "vector_index.db")
            vec_idx_path = str(plugin_instance.cache_dir / "vector_index.faiss")
            await vector_svc.initialize(emb_provider, vec_db_path, vec_idx_path)
            logger.info("[VectorIndex] 手动初始化完成")

        # 重建索引
        idx = await plugin_instance._load_index()
        if not idx:
            yield "❌ 贴图索引为空，没有可导入的贴图。"
            return

        total = len(idx)
        yield f"⏳ 正在重建向量索引，共 {total} 张贴图..."

        ok, count = await vector_svc.rebuild_from_index(idx)
        yield f"✅ 向量索引重建完成：成功 {ok}/{count} 条"

    except Exception as e:
        logger.error(f"[VectorIndex] 手动重建失败: {e}", exc_info=True)
        yield f"❌ 重建失败: {e}"