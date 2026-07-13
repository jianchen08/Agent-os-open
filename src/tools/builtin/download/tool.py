"""
通用文件下载工具

基于 httpx 封装，支持多连接分段下载、断点续传、自动重试、安全限制。

暴露接口：
- DownloadTool：通用下载工具类
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx

from tools.builtin.base import BuiltinTool
from tools.common.ssrf_guard import validate_url as _validate_url
from tools.types import (
    Tool,
    ToolCategory,
    ToolLevel,
    ToolResult,
    ToolSource,
    create_failure_result,
    create_success_result,
)

logger = logging.getLogger(__name__)

# ─── 常量 ───────────────────────────────────────────────
DEFAULT_MAX_SIZE = 1 * 1024 * 1024 * 1024  # 1 GB
DEFAULT_SEGMENT_SIZE = 4 * 1024 * 1024  # 4 MB per segment
DEFAULT_MAX_CONNECTIONS = 8
DEFAULT_MAX_RETRIES = 5
DEFAULT_TIMEOUT = 300
MAX_REDIRECTS = 5
CHUNK_SIZE = 64 * 1024  # 64 KB read/write buffer

# SSRF 防护（协议/域名白名单 + DNS 解析内网 IP 检查）抽到公共模块，
# download / web 等工具共用，避免漏接。详见 tools.common.ssrf_guard。


def _sanitize_filename(name: str) -> str:
    """清洗文件名：去除路径分隔符，防止路径穿越"""
    name = os.path.basename(name)  # noqa: PTH119
    # 仅保留安全字符
    name = re.sub(r"[^\w\s\.\-\u4e00-\u9fff]", "_", name)
    # 去除前后空白和点
    name = name.strip(". \t\n\r")
    if not name:
        name = "download"
    return name


def _extract_filename_from_url(url: str) -> str:
    """从 URL 中提取文件名"""
    parsed = urlparse(url)
    path = unquote(parsed.path)
    name = os.path.basename(path)  # noqa: PTH119
    if name:
        return _sanitize_filename(name)
    return "download"


def _extract_filename_from_headers(headers: httpx.Headers) -> str | None:
    """从 Content-Disposition 头提取文件名"""
    cd = headers.get("content-disposition", "")
    if not cd:
        return None
    # 尝试 filename*= (RFC 5987)
    match = re.search(r"filename\*=(?:UTF-8|utf-8)?''(.+?)(?:;|$)", cd)
    if match:
        return _sanitize_filename(unquote(match.group(1)))
    # 尝试 filename=
    match = re.search(r'filename="?(.+?)"?(?:;|$)', cd)
    if match:
        return _sanitize_filename(match.group(1))
    return None


def _format_size(size_bytes: float) -> str:
    """格式化文件大小"""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size_bytes) < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} PB"


class DownloadTool(BuiltinTool):
    """
    通用文件下载工具

    基于 httpx 封装，支持：
    - 多连接分段下载（Range 请求）
    - 断点续传（.part 文件 + 状态 JSON）
    - 自动重试（指数退避）
    - 安全限制（大小上限/域名白名单/SSRF 防护/路径穿越防护）
    """

    @staticmethod
    def get_tool_definition() -> Tool:
        return Tool(
            name="download",
            description=(
                "通用文件下载工具。支持多连接分段下载、断点续传、自动重试。"
                "适用场景：从网络下载文件到本地。"
                "不适用场景：已有本地文件操作（使用 file_read/file_write）。"
                "安全限制：仅允许 http/https 协议，默认文件大小上限 1GB，内置 SSRF 防护。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "下载地址（http/https），必填",
                    },
                    "save_path": {
                        "type": "string",
                        "description": "保存目录路径（必填），文件名自动从 URL 或响应头获取",
                    },
                    "filename": {
                        "type": "string",
                        "description": "指定文件名（可选，默认自动检测）",
                    },
                    "max_connections": {
                        "type": "integer",
                        "description": "最大分段/连接数，默认 8",
                        "default": 8,
                    },
                    "max_retries": {
                        "type": "integer",
                        "description": "最大重试次数，默认 5",
                        "default": 5,
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "超时秒数，默认 300",
                        "default": 300,
                    },
                    "max_size": {
                        "type": "integer",
                        "description": "文件大小上限（字节），默认 1GB，设为 0 表示不限制",
                        "default": 1073741824,
                    },
                    "proxy": {
                        "type": "string",
                        "description": "代理地址（可选，如 http://127.0.0.1:7890）",
                    },
                    "allow_domains": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "域名白名单（可选，如 ['github.com', 'pypi.org']）",
                    },
                    "expected_hash": {
                        "type": "string",
                        "description": "期望的 SHA256 哈希值（可选，下载后校验）",
                    },
                    "skip_ssrf_check": {
                        "type": "boolean",
                        "description": "跳过 SSRF 内网 IP 检查（默认 false，仅用于测试本地服务器）",
                        "default": False,
                    },
                },
                "required": ["url", "save_path"],
            },
            category=ToolCategory.FILE,
            level=ToolLevel.ALL,
            source=ToolSource.BUILTIN,
            examples=[
                {
                    "input": {
                        "url": "https://example.com/file.zip",
                        "save_path": "/tmp/downloads",
                    },
                    "output": {
                        "success": True,
                        "path": "/tmp/downloads/file.zip",
                        "size": 1048576,
                        "duration": 2.5,
                        "avg_speed": "400.0 KB/s",
                    },
                    "description": "下载文件到指定目录",
                },
            ],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolResult:  # noqa: PLR0911
        """执行下载"""
        url = inputs.get("url", "").strip()
        save_path = inputs.get("save_path", "").strip()
        filename = inputs.get("filename") or None
        max_connections = min(int(inputs.get("max_connections", DEFAULT_MAX_CONNECTIONS)), 32)
        max_retries = int(inputs.get("max_retries", DEFAULT_MAX_RETRIES))
        timeout = int(inputs.get("timeout", DEFAULT_TIMEOUT))
        max_size = int(inputs.get("max_size", DEFAULT_MAX_SIZE))
        proxy = inputs.get("proxy") or None
        allow_domains = inputs.get("allow_domains") or None
        expected_hash = inputs.get("expected_hash") or None
        skip_ssrf_check = bool(inputs.get("skip_ssrf_check", False))

        # ── 参数校验 ──
        if not url:
            return create_failure_result("缺少必填参数: url")
        if not save_path:
            return create_failure_result("缺少必填参数: save_path")

        # ── URL 安全校验 ──
        if skip_ssrf_check:
            # skip_ssrf_check 仅用于测试本地开发服务器，绝不能旁路到内网。
            # 即便 skip，也强制拦截"主机名本身是内网 IP 字面量"的情况
            # （http://169.254.169.254、http://10.0.0.1 等直连），
            # 避免攻击者用一个布尔位就关掉 SSRF 防护访问云 metadata。
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https"):
                return create_failure_result(f"URL 安全校验失败: 不支持的协议: {parsed.scheme}")
            hostname = parsed.hostname
            if not hostname:
                return create_failure_result("URL 安全校验失败: URL 缺少主机名")
            # 主机名是 IP 字面量且属于内网 → 拒绝（skip 也不能放行）
            from tools.common.ssrf_guard import is_private_ip  # noqa: PLC0415

            if is_private_ip(hostname):
                return create_failure_result(
                    f"URL 安全校验失败: skip_ssrf_check 也不能访问内网 IP {hostname}"
                )
        else:
            valid, msg = _validate_url(url, allow_domains)
            if not valid:
                return create_failure_result(f"URL 安全校验失败: {msg}")

        # ── 创建保存目录 ──
        save_dir = Path(save_path)
        try:
            save_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return create_failure_result(f"创建保存目录失败: {e}")

        start_time = time.monotonic()

        try:
            result = await self._download(
                url=url,
                save_dir=save_dir,
                filename=filename,
                max_connections=max_connections,
                max_retries=max_retries,
                timeout=timeout,
                max_size=max_size,
                proxy=proxy,
            )

            elapsed = time.monotonic() - start_time

            # ── 哈希校验 ──
            if expected_hash and result.get("path"):
                actual_hash = await self._sha256_file(result["path"])
                if actual_hash != expected_hash.lower():
                    return create_failure_result(f"哈希校验失败: 期望 {expected_hash}, 实际 {actual_hash}")

            # ── 构建返回结果 ──
            file_size = result.get("size", 0)
            avg_speed = _format_size(file_size / elapsed) + "/s" if elapsed > 0 else "N/A"

            return create_success_result(
                data={
                    "success": True,
                    "path": str(result.get("path", "")),
                    "size": file_size,
                    "duration": round(elapsed, 2),
                    "avg_speed": avg_speed,
                    "error": None,
                },
                metadata={
                    "url": url,
                    "filename": os.path.basename(str(result.get("path", ""))),  # noqa: PTH119
                    "segments": result.get("segments", 1),
                    "resumed": result.get("resumed", False),
                },
            )

        except Exception as e:
            elapsed = time.monotonic() - start_time
            logger.error(f"下载失败: {url} - {e}")
            return create_failure_result(f"下载失败: {e}")

    # ────────────────────────────────────────────────────────
    # 核心下载逻辑
    # ────────────────────────────────────────────────────────

    async def _download(
        self,
        url: str,
        save_dir: Path,
        filename: str | None,
        max_connections: int,
        max_retries: int,
        timeout: int,
        max_size: int,
        proxy: str | None,
    ) -> dict:
        """
        核心下载逻辑：
        1. HEAD 探测文件信息和 Range 支持情况
        2. 检查是否有断点续传状态
        3. 选择分段下载或单连接流式下载
        4. 合并分片并原子重命名
        """

        client_kwargs = {
            "follow_redirects": True,
            "max_redirects": MAX_REDIRECTS,
            "timeout": httpx.Timeout(timeout, connect=30),
            "verify": True,  # TLS 校验
        }
        if proxy:
            client_kwargs["proxy"] = proxy

        async with httpx.AsyncClient(**client_kwargs) as client:
            # ── Step 1: HEAD 探测 ──
            content_length = 0
            accept_ranges = False
            etag = ""
            head_headers = httpx.Headers()
            try:
                head_resp = await self._retry_request(client.head, url, max_retries=min(max_retries, 2))
                content_length = int(head_resp.headers.get("content-length", 0))
                accept_ranges = head_resp.headers.get("accept-ranges", "").lower() == "bytes"
                etag = head_resp.headers.get("etag", "")
                head_headers = head_resp.headers
            except Exception as e:
                logger.warning(f"HEAD 请求失败，将回退到流式 GET 下载: {e}")

            # 确定文件名（无论 HEAD 是否成功都需要）
            if not filename:
                filename = _extract_filename_from_headers(head_headers) or _extract_filename_from_url(url)
            filename = _sanitize_filename(filename)

            final_path = save_dir / filename
            state_path = save_dir / f"{filename}.state.json"

            # HEAD 失败 → 直接走流式 GET
            if content_length == 0 and not accept_ranges:
                return await self._stream_download(
                    client=client,
                    url=url,
                    final_path=final_path,
                    state_path=state_path,
                    max_retries=max_retries,
                    max_size=max_size,
                    content_length=content_length,
                )

            # ── 文件大小校验 ──
            if max_size > 0 and content_length > 0 and content_length > max_size:
                raise ValueError(f"文件大小 {content_length} 字节超过上限 {max_size} 字节({_format_size(max_size)})")

            # ── Step 2: 检查断点续传状态 ──
            resumed = False
            segments_state = None
            if state_path.exists() and final_path.exists() is False:
                try:
                    segments_state = json.loads(state_path.read_text(encoding="utf-8"))
                    resumed = True
                    logger.info(f"检测到断点续传状态: {state_path}")
                except (json.JSONDecodeError, OSError) as e:
                    logger.warning(f"状态文件损坏，忽略续传: {e}")
                    segments_state = None

            # ── Step 3: 选择下载方式 ──
            if accept_ranges and content_length > 0:
                # 分段下载
                result = await self._segmented_download(
                    client=client,
                    url=url,
                    save_dir=save_dir,
                    filename=filename,
                    content_length=content_length,
                    etag=etag,
                    max_connections=max_connections,
                    max_retries=max_retries,
                    max_size=max_size,
                    segments_state=segments_state,
                )
                result["resumed"] = resumed
                return result
            # 不支持 Range 或未知大小 → 单连接流式
            return await self._stream_download(
                client=client,
                url=url,
                final_path=final_path,
                state_path=state_path,
                max_retries=max_retries,
                max_size=max_size,
                content_length=content_length,
            )

    async def _segmented_download(  # noqa: PLR0915
        self,
        client: httpx.AsyncClient,
        url: str,
        save_dir: Path,
        filename: str,
        content_length: int,
        etag: str,
        max_connections: int,
        max_retries: int,
        max_size: int,
        segments_state: dict | None,
    ) -> dict:
        """分段并发下载（Range 请求）"""

        segment_size = max(DEFAULT_SEGMENT_SIZE, content_length // max_connections)
        num_segments = max(1, min(max_connections, (content_length + segment_size - 1) // segment_size))

        # 调整实际分段大小
        actual_seg_size = content_length // num_segments

        # 计算每段的字节范围
        segments = []
        for i in range(num_segments):
            start = i * actual_seg_size
            end = (start + actual_seg_size - 1) if i < num_segments - 1 else (content_length - 1)
            segments.append({"index": i, "start": start, "end": end})

        # 检查续传：哪些分片已完成
        completed_segments = set()
        if segments_state and segments_state.get("etag") == etag:
            for seg in segments_state.get("completed_segments", []):
                completed_segments.add(seg)

        # 保存状态文件
        state_path = save_dir / f"{filename}.state.json"
        final_path = save_dir / filename

        def _save_state(completed: set[int]):
            state = {
                "url": url,
                "filename": filename,
                "content_length": content_length,
                "etag": etag,
                "num_segments": num_segments,
                "completed_segments": sorted(completed),
                "updated_at": time.time(),
            }
            state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

        # ── 并发下载各分片 ──
        sem = asyncio.Semaphore(max_connections)

        async def _fetch_segment(seg: dict) -> None:
            seg_idx = seg["index"]
            start = seg["start"]
            end = seg["end"]

            # 已完成的分片跳过
            if seg_idx in completed_segments:
                part_file = save_dir / f"{filename}.part.{seg_idx}"
                if part_file.exists() and part_file.stat().st_size == (end - start + 1):
                    return

            part_file = save_dir / f"{filename}.part.{seg_idx}"

            async with sem:
                for attempt in range(1, max_retries + 1):
                    try:
                        headers = {"Range": f"bytes={start}-{end}"}
                        resp = await client.get(url, headers=headers)
                        resp.raise_for_status()

                        # 写入分片临时文件
                        with open(part_file, "wb") as f:
                            async for chunk in resp.aiter_bytes(CHUNK_SIZE):
                                f.write(chunk)

                        # 验证分片大小
                        actual_size = part_file.stat().st_size
                        expected_size = end - start + 1
                        if actual_size != expected_size:
                            raise OSError(f"分片 {seg_idx} 大小不匹配: 期望 {expected_size}, 实际 {actual_size}")

                        completed_segments.add(seg_idx)
                        _save_state(completed_segments)
                        logger.debug(f"分片 {seg_idx}/{num_segments} 完成")
                        return

                    except Exception as e:
                        if attempt < max_retries:
                            wait = min(2**attempt, 30)  # 指数退避，最多 30 秒
                            logger.warning(f"分片 {seg_idx} 第 {attempt} 次重试: {e}，等待 {wait}s")
                            await asyncio.sleep(wait)
                        else:
                            raise RuntimeError(  # noqa: B904
                                f"分片 {seg_idx} 下载失败（已重试 {max_retries} 次）: {e}"
                            )

        # 启动并发下载
        tasks = [_fetch_segment(seg) for seg in segments]
        await asyncio.gather(*tasks)

        # ── 合并分片 ──
        with open(final_path, "wb") as out:
            for seg in segments:
                part_file = save_dir / f"{filename}.part.{seg['index']}"
                with open(part_file, "rb") as pf:
                    while True:
                        chunk = pf.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        out.write(chunk)
                os.remove(part_file)  # noqa: PTH107

        # 清理状态文件
        if state_path.exists():
            os.remove(state_path)  # noqa: PTH107

        actual_size = final_path.stat().st_size
        logger.info(f"下载完成: {final_path} ({_format_size(actual_size)})，共 {num_segments} 个分片")

        return {
            "path": final_path,
            "size": actual_size,
            "segments": num_segments,
            "resumed": False,
        }

    async def _stream_download(
        self,
        client: httpx.AsyncClient,
        url: str,
        final_path: Path,
        state_path: Path,
        max_retries: int,
        max_size: int,
        content_length: int,
    ) -> dict:
        """单连接流式下载（不支持 Range 或未知大小时的回退方案）"""

        # 检查是否有部分下载
        resume_from = 0
        temp_path = Path(str(final_path) + ".tmp")

        if temp_path.exists() and content_length > 0:
            resume_from = temp_path.stat().st_size
            if resume_from >= content_length:
                # 文件已完整，直接重命名
                temp_path.rename(final_path)
                return {"path": final_path, "size": content_length, "segments": 1}

        for attempt in range(1, max_retries + 1):
            try:
                headers = {}
                if resume_from > 0:
                    headers["Range"] = f"bytes={resume_from}-"

                resp = await client.get(url, headers=headers)
                resp.raise_for_status()

                total_downloaded = resume_from

                mode = "ab" if resume_from > 0 else "wb"
                with open(temp_path, mode) as f:
                    async for chunk in resp.aiter_bytes(CHUNK_SIZE):
                        total_downloaded += len(chunk)

                        # 流式大小校验
                        if max_size > 0 and total_downloaded > max_size:
                            raise ValueError(f"下载已超过大小上限 {max_size} 字节")

                        f.write(chunk)

                # 重命名为最终文件名
                temp_path.rename(final_path)

                actual_size = final_path.stat().st_size
                logger.info(f"下载完成: {final_path} ({_format_size(actual_size)})")

                return {
                    "path": final_path,
                    "size": actual_size,
                    "segments": 1,
                    "resumed": resume_from > 0,
                }

            except Exception as e:
                if attempt < max_retries:
                    wait = min(2**attempt, 30)
                    logger.warning(f"第 {attempt} 次重试: {e}，等待 {wait}s")
                    await asyncio.sleep(wait)
                else:
                    raise RuntimeError(f"下载失败（已重试 {max_retries} 次）: {e}")  # noqa: B904
        return None

    # ────────────────────────────────────────────────────────
    # 工具方法
    # ────────────────────────────────────────────────────────

    async def _retry_request(self, method, url: str, max_retries: int = 3, **kwargs) -> httpx.Response:
        """带指数退避的重试请求"""
        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                resp = await method(url, **kwargs)
                resp.raise_for_status()
                return resp
            except Exception as e:
                last_error = e
                if attempt < max_retries:
                    wait = min(2**attempt, 30)
                    logger.debug(f"请求重试 {attempt}/{max_retries}: {e}，等待 {wait}s")
                    await asyncio.sleep(wait)
        raise RuntimeError(f"请求失败（已重试 {max_retries} 次）: {last_error}")

    @staticmethod
    async def _sha256_file(path: Path) -> str:
        """计算文件的 SHA256 哈希"""
        sha = hashlib.sha256()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                sha.update(chunk)
        return sha.hexdigest()
