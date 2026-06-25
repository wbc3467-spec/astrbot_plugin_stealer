"""图片下载服务：负责 HTTP 下载、连接池管理和超时处理。"""

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Any

import aiohttp

from astrbot.api import logger


class ImageDownloadService:
    """基于 aiohttp 的图片下载服务，复用连接池。"""

    HTTP_TIMEOUT_SECONDS = 30
    HTTP_CONNECTOR_LIMIT = 10
    HTTP_CONNECTOR_LIMIT_PER_HOST = 5
    HTTP_DNS_CACHE_SECONDS = 300

    def __init__(self, plugin_instance: Any = None):
        self.plugin = plugin_instance
        self._aiohttp_session: aiohttp.ClientSession | None = None

    async def get_session(self) -> aiohttp.ClientSession:
        """获取或创建共享的 aiohttp session。"""
        if self._aiohttp_session is None or self._aiohttp_session.closed:
            connector = aiohttp.TCPConnector(
                limit=self.HTTP_CONNECTOR_LIMIT,
                limit_per_host=self.HTTP_CONNECTOR_LIMIT_PER_HOST,
                ttl_dns_cache=self.HTTP_DNS_CACHE_SECONDS,
                use_dns_cache=True,
            )
            timeout = aiohttp.ClientTimeout(total=self.HTTP_TIMEOUT_SECONDS)
            self._aiohttp_session = aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
            )
        return self._aiohttp_session

    async def close(self) -> None:
        """关闭共享的 aiohttp session。"""
        if self._aiohttp_session and not self._aiohttp_session.closed:
            await self._aiohttp_session.close()
            self._aiohttp_session = None

    def build_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.plugin is not None:
            napcat_token = getattr(self.plugin, "napcat_token", "")
            if napcat_token:
                headers["Authorization"] = f"Bearer {napcat_token}"
        return headers

    @staticmethod
    def detect_file_type(content_type: str, content: bytes) -> tuple[str, bool]:
        content_type = str(content_type or "").lower()
        is_gif = "gif" in content_type or content[:6] in (b"GIF89a", b"GIF87a")
        if is_gif:
            return ".gif", True
        if "png" in content_type or content[:8] == b"\x89PNG\r\n\x1a\n":
            return ".png", False
        if "webp" in content_type or (content[:4] == b"RIFF" and content[8:12] == b"WEBP"):
            return ".webp", False
        if "jpeg" in content_type or "jpg" in content_type:
            return ".jpg", False
        return ".jpg", False

    async def download_to_temp(
        self, url: str, *, log_download: bool = False
    ) -> tuple[str | None, bool]:
        """从 URL 下载文件到临时文件。

        Returns:
            tuple[str | None, bool]: (临时文件路径, 是否为GIF动图)，失败返回 (None, False)
        """
        if not url or not isinstance(url, str):
            return None, False

        try:
            session = await self.get_session()
            async with session.get(
                url,
                headers=self.build_headers(),
                timeout=aiohttp.ClientTimeout(total=self.HTTP_TIMEOUT_SECONDS),
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"下载图片失败: HTTP {resp.status}")
                    return None, False

                content_type = resp.headers.get("Content-Type", "").lower()
                content = await resp.read()
                ext, is_gif = self.detect_file_type(content_type, content)

                temp_fd, temp_path = tempfile.mkstemp(suffix=ext)
                try:
                    os.write(temp_fd, content)
                    if log_download:
                        logger.debug(
                            f"已下载原始图片: {temp_path} ({len(content)} bytes, "
                            f"type={content_type}, is_gif={is_gif})"
                        )
                    return temp_path, is_gif
                finally:
                    os.close(temp_fd)
        except asyncio.TimeoutError:
            logger.warning("下载图片超时")
            return None, False
        except Exception as e:
            logger.warning(f"下载图片失败: {e}")
            return None, False

    async def download_original_image(self, img: Any) -> tuple[str | None, bool]:
        """下载原始图片文件。

        Args:
            img: 图片组件

        Returns:
            tuple[str | None, bool]: (临时文件路径, 是否为GIF动图)，失败返回 (None, False)
        """
        # 优先使用 Image 组件自带的 convert_to_file_path 方法喵！
        if hasattr(img, 'convert_to_file_path'):
            try:
                file_path = await img.convert_to_file_path()
                if file_path and Path(file_path).exists():
                    is_gif = file_path.lower().endswith(".gif")
                    logger.debug(f"[图片下载] convert_to_file_path 成功: {file_path}")
                    return file_path, is_gif
            except Exception as e:
                logger.debug(f"[图片下载] convert_to_file_path 失败: {e}")
        
        # 备用：手动检查属性喵
        url = getattr(img, "url", "") or getattr(img, "file", "")
        if url:
            return await self.download_to_temp(url, log_download=True)
        
        # 最后手段：检查 path 属性喵
        file_path = getattr(img, "path", "") or getattr(img, "file_path", "")
        if file_path and Path(file_path).exists():
            is_gif = file_path.lower().endswith(".gif")
            return file_path, is_gif
        
        logger.debug(f"[图片下载] 所有方式都失败了喵，img.dir部分属性: {[a for a in dir(img) if not a.startswith('_')][:15]}")
        return None, False

    async def download_url_to_temp(self, url: str) -> tuple[str | None, bool]:
        """从 URL 下载文件到临时文件，返回 (temp_path, is_gif)。"""
        return await self.download_to_temp(url)
