"""
向量索引服务 - 基于 FaissVecDB 的贴图向量检索
为贴图搜索引擎提供语义相似度搜索能力
"""

import asyncio
from typing import Any

from astrbot.api import logger
from astrbot.core.db.vec_db.faiss_impl.vec_db import FaissVecDB
from astrbot.core.provider.provider import EmbeddingProvider


class VectorIndexService:
    """贴图向量索引服务，包装 FaissVecDB 提供语义搜索能力。"""

    def __init__(self):
        self.faiss_db: FaissVecDB | None = None
        self._embedding_provider: EmbeddingProvider | None = None
        self._file_path_to_doc_id: dict[str, int] = {}
        self._doc_id_to_file_path: dict[int, str] = {}
        self._initialized = False

    async def initialize(
        self,
        embedding_provider: EmbeddingProvider,
        db_path: str,
        index_path: str,
    ) -> None:
        """初始化 FaissVecDB，加载或创建向量索引。"""
        self._embedding_provider = embedding_provider
        self.faiss_db = FaissVecDB(db_path, index_path, embedding_provider)
        await self.faiss_db.initialize()
        self._initialized = True
        logger.info("[VectorIndex] 向量索引服务已初始化")

    @staticmethod
    def _build_entry_text(entry_data: dict[str, Any]) -> str:
        """将贴图条目数据拼接为索引文本。"""
        category = str(entry_data.get("category", "") or "")
        desc = str(entry_data.get("desc", "") or "")
        tags = entry_data.get("tags", []) or []
        scenes = entry_data.get("scenes", []) or []

        if isinstance(tags, str):
            tags = [tags]
        if isinstance(scenes, str):
            scenes = [scenes]

        parts = [category, desc] + list(tags) + list(scenes)
        return " ".join(p for p in parts if p and str(p).strip())

    async def index_entry(
        self,
        file_path: str,
        entry_data: dict[str, Any],
    ) -> None:
        """索引一张贴图：插入或更新 FAISS。"""
        if not self._initialized or self.faiss_db is None:
            logger.debug("[VectorIndex] 未初始化，跳过索引")
            return

        text = self._build_entry_text(entry_data)
        if not text.strip():
            logger.debug(f"[VectorIndex] 贴图 '{file_path}' 无文本内容，跳过")
            return

        try:
            if file_path in self._file_path_to_doc_id:
                await self.remove_entry(file_path)

            metadata = {
                "file_path": file_path,
                "category": str(entry_data.get("category", "") or ""),
            }
            doc_id = await self.faiss_db.insert(
                content=text,
                metadata=metadata,
            )
            self._file_path_to_doc_id[file_path] = doc_id
            self._doc_id_to_file_path[doc_id] = file_path
            logger.debug(f"[VectorIndex] 已索引: {file_path} -> doc_id={doc_id}")

        except Exception as e:
            logger.error(f"[VectorIndex] 索引失败 '{file_path}': {e}")

    async def remove_entry(self, file_path: str) -> None:
        """从向量索引删除一张贴图。"""
        if not self._initialized or self.faiss_db is None:
            return

        doc_id = self._file_path_to_doc_id.pop(file_path, None)
        if doc_id is not None:
            try:
                # FaissVecDB.delete() 需要 UUID 字符串，从文档存储查询
                uuid_doc_id = await self._get_uuid_from_int_id(doc_id)
                if uuid_doc_id:
                    await self.faiss_db.delete(uuid_doc_id)
                self._doc_id_to_file_path.pop(doc_id, None)
                logger.debug(f"[VectorIndex] 已删除: {file_path}")
            except Exception as e:
                logger.error(f"[VectorIndex] 删除失败 '{file_path}': {e}")

    async def _get_uuid_from_int_id(self, int_id: int) -> str | None:
        """通过整型 ID 查询 UUID。"""
        try:
            docs = await self.faiss_db.document_storage.get_documents(
                metadata_filters={}, ids=[int_id], limit=1
            )
            if docs and len(docs) > 0:
                return docs[0].get("doc_id")
        except Exception as e:
            logger.debug(f"[VectorIndex] UUID 查询失败 (id={int_id}): {e}")
        return None

    async def search(
        self,
        query: str,
        k: int = 10,
    ) -> list[tuple[str, float]]:
        """向量语义搜索，返回 (file_path, similarity_score) 列表。"""
        if not self._initialized or self.faiss_db is None:
            return []

        if not query or not query.strip():
            return []

        try:
            results = await self.faiss_db.retrieve(
                query=query.strip(),
                k=k,
                fetch_k=k * 2,
                rerank=False,
            )

            output = []
            for result in results:
                doc_data = result.data
                # metadata 字段是 JSON 字符串，需要解析
                metadata_raw = doc_data.get("metadata", "{}")
                if isinstance(metadata_raw, str):
                    import json
                    try:
                        metadata = json.loads(metadata_raw)
                    except json.JSONDecodeError:
                        metadata = {}
                else:
                    metadata = metadata_raw
                file_path = metadata.get("file_path", "") if isinstance(metadata, dict) else ""
                if file_path:
                    output.append((file_path, result.similarity))

            return output

        except Exception as e:
            logger.error(f"[VectorIndex] 搜索失败 '{query}': {e}")
            return []

    async def rebuild_from_index(
        self,
        idx: dict[str, Any],
        *,
        max_concurrent: int = 5,
    ) -> tuple[int, int]:
        """从完整索引重建 FAISS。"""
        if not self._initialized or self.faiss_db is None:
            return (0, 0)

        self._file_path_to_doc_id.clear()
        self._doc_id_to_file_path.clear()

        entries = []
        for file_path, entry_data in idx.items():
            if not isinstance(entry_data, dict):
                continue
            text = self._build_entry_text(entry_data)
            if text.strip():
                entries.append((file_path, entry_data, text))

        if not entries:
            return (0, 0)

        sem = asyncio.Semaphore(max_concurrent)
        success = 0

        async def _index_one(fp: str, ed: dict[str, Any], txt: str):
            nonlocal success
            async with sem:
                try:
                    metadata = {
                        "file_path": fp,
                        "category": str(ed.get("category", "") or ""),
                    }
                    doc_id = await self.faiss_db.insert(content=txt, metadata=metadata)
                    self._file_path_to_doc_id[fp] = doc_id
                    self._doc_id_to_file_path[doc_id] = fp
                    success += 1
                except Exception as e:
                    logger.error(f"[VectorIndex] 重建索引失败 '{fp}': {e}")

        await asyncio.gather(*[_index_one(fp, ed, txt) for fp, ed, txt in entries])

        logger.info(f"[VectorIndex] 重建完成: {success}/{len(entries)} 条")
        return (success, len(entries))

    async def load_existing_entries(self) -> int:
        """从 FAISS 文档存储加载已有条目到内存缓存。
        避免启动时因缓存为空导致重复索引。
        """
        if not self._initialized or self.faiss_db is None:
            return 0
        try:
            docs = await self.faiss_db.document_storage.get_documents(
                metadata_filters={},
                offset=None,
                limit=None,
            )
            count = 0
            for doc in docs:
                metadata = doc.get("metadata", {})
                if isinstance(metadata, str):
                    try:
                        import json
                        metadata = json.loads(metadata)
                    except Exception:
                        continue
                file_path = metadata.get("file_path", "")
                if file_path:
                    self._file_path_to_doc_id[file_path] = doc["id"]
                    self._doc_id_to_file_path[doc["id"]] = file_path
                    count += 1
            logger.info(f"[VectorIndex] 从已存在索引加载了 {count} 条映射")
            return count
        except Exception as e:
            logger.warning(f"[VectorIndex] 加载已有条目失败: {e}")
            return 0

    async def close(self) -> None:
        """关闭向量索引，释放资源。"""
        self._initialized = False
        self._file_path_to_doc_id.clear()
        self._doc_id_to_file_path.clear()

        if self.faiss_db is not None:
            try:
                if hasattr(self.faiss_db, "close"):
                    await self.faiss_db.close()
            except Exception as e:
                logger.error(f"[VectorIndex] 关闭失败: {e}")

        self.faiss_db = None
        self._embedding_provider = None
        logger.info("[VectorIndex] 向量索引服务已关闭")