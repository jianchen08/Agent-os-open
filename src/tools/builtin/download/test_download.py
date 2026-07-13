"""
通用下载工具 - 真实下载测试

使用本地 HTTP 服务器确保测试可靠，同时保留外部 URL 测试（标记为 network）。

测试用例：
1. 小文件下载 + 完整性校验（SHA256）
2. 中等文件分段下载（验证分片合并正确）
3. 断点续传测试
4. 超时处理测试
5. 安全校验测试（SSRF、路径穿越、协议限制）
6. 外部网络真实下载测试（可选）
"""

import asyncio  # noqa: F401
import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import pytest

from .tool import DownloadTool, _is_private_ip, _sanitize_filename, _validate_url

# ─── 本地 HTTP 测试服务器 ────────────────────────────


class _SilentHandler(SimpleHTTPRequestHandler):
    """静默 HTTP 处理器（不打印日志）"""

    def do_HEAD(self, *args, **kwargs):
        """支持 HEAD 请求（父类默认不支持）"""
        self.do_GET(*args, **kwargs)

    def end_headers(self):
        # 添加 Range 支持 header
        if not self.send_header_only_if_exists:
            self.send_header("Accept-Ranges", "bytes")
        super().end_headers()

    def log_message(self, format, *args):
        pass  # 静默日志


@pytest.fixture(scope="module")
def test_server(tmp_path_factory):
    """启动本地 HTTP 测试服务器"""
    # 创建测试文件目录
    test_dir = tmp_path_factory.mktemp("http_root")

    # 1. 小文件 (1KB 随机数据)
    small_data = os.urandom(1024)
    (test_dir / "small.bin").write_bytes(small_data)

    # 2. 中等文件 (5MB，4个分片)
    medium_data = os.urandom(5 * 1024 * 1024)
    (test_dir / "medium.bin").write_bytes(medium_data)

    # 3. 1 字节文件（用于大小限制测试）
    (test_dir / "tiny.bin").write_bytes(b"X")

    # 启动 HTTP 服务器
    handler = type("Handler", (_SilentHandler,), {"directory": str(test_dir)})
    server = HTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"

    yield {
        "base_url": base_url,
        "test_dir": test_dir,
        "small_data": small_data,
        "small_sha256": hashlib.sha256(small_data).hexdigest(),
        "medium_data": medium_data,
        "medium_sha256": hashlib.sha256(medium_data).hexdigest(),
    }

    server.shutdown()


@pytest.fixture
def download_dir():
    """创建临时下载目录"""
    d = tempfile.mkdtemp(prefix="download_test_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def tool():
    return DownloadTool()


# ─── 辅助函数 ──────────────────────────────────────


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    sha = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(64 * 1024)
            if not chunk:
                break
            sha.update(chunk)
    return sha.hexdigest()


# ─── 1. 小文件下载 + 完整性校验 ───────────────────────


@pytest.mark.asyncio
async def test_download_small_file_with_hash(tool, download_dir, test_server):
    """下载小文件 (1KB) 并用 SHA256 校验完整性"""
    url = f"{test_server['base_url']}/small.bin"
    expected_hash = test_server["small_sha256"]

    result = await tool.execute(
        {
            "url": url,
            "save_path": download_dir,
            "timeout": 30,
            "expected_hash": expected_hash,
            "skip_ssrf_check": True,
        }
    )

    print(f"\n[小文件下载] 结果: success={result.success}")

    assert result.success, f"下载失败: {result.error}"
    data = result.output
    assert data["success"] is True
    assert data["size"] == 1024, f"期望 1024 字节，实际 {data['size']}"
    assert data["path"], "文件路径为空"

    # 验证文件实际存在
    file_path = Path(data["path"])
    assert file_path.exists(), f"文件不存在: {file_path}"

    # 验证 SHA256
    actual_hash = _sha256_file(file_path)
    assert actual_hash == expected_hash, f"SHA256 不匹配: {actual_hash} != {expected_hash}"

    print(f"  文件: {file_path.name}")
    print(f"  大小: {data['size']} 字节")
    print(f"  耗时: {data['duration']}s")
    print(f"  速度: {data['avg_speed']}")
    print(f"  SHA256: {actual_hash[:16]}...")


# ─── 2. 中等文件分段下载 ─────────────────────────────


@pytest.mark.asyncio
async def test_download_medium_file_segmented(tool, download_dir, test_server):
    """下载中等文件 (5MB)，验证分段下载和分片合并正确"""
    url = f"{test_server['base_url']}/medium.bin"
    expected_hash = test_server["medium_sha256"]
    expected_size = len(test_server["medium_data"])

    result = await tool.execute(
        {
            "url": url,
            "save_path": download_dir,
            "filename": "medium_test.bin",
            "max_connections": 4,
            "timeout": 60,
            "skip_ssrf_check": True,
        }
    )

    print(f"\n[中等文件分段下载] 结果: success={result.success}")

    assert result.success, f"下载失败: {result.error}"
    data = result.output
    assert data["success"] is True
    assert data["size"] == expected_size, f"大小不匹配: {data['size']} vs {expected_size}"

    # 验证分段信息
    meta = result.metadata
    assert meta.get("segments", 1) > 1, f"应使用分段下载，实际分段数: {meta.get('segments')}"

    # 验证 SHA256（分片合并正确性）
    file_path = Path(data["path"])
    actual_hash = _sha256_file(file_path)
    assert actual_hash == expected_hash, f"合并后 SHA256 不匹配: {actual_hash} != {expected_hash}"

    # 验证无残留文件
    part_files = list(Path(download_dir).glob("*.part.*"))
    assert len(part_files) == 0, f"存在残留分片: {part_files}"
    state_files = list(Path(download_dir).glob("*.state.json"))
    assert len(state_files) == 0, f"存在残留状态: {state_files}"

    print(f"  文件: {file_path.name}")
    print(f"  大小: {data['size']} 字节 ({data['size'] / 1024 / 1024:.1f} MB)")
    print(f"  分段数: {meta.get('segments')}")
    print(f"  耗时: {data['duration']}s")
    print(f"  速度: {data['avg_speed']}")
    print(f"  SHA256: {actual_hash[:16]}...")


# ─── 3. 断点续传测试 ────────────────────────────────


@pytest.mark.asyncio
async def test_resume_download(tool, download_dir, test_server):
    """
    断点续传测试：
    1. 手动创建部分分片 + 状态文件（模拟下载中断）
    2. 重新下载，验证从断点恢复
    3. 验证最终文件完整性
    """
    url = f"{test_server['base_url']}/medium.bin"
    filename = "resume_test.bin"
    expected_hash = test_server["medium_sha256"]
    expected_size = len(test_server["medium_data"])

    import httpx  # noqa: PLC0415

    # Step 1: 手动下载第 0 个分片，模拟中断
    segment_size = 4 * 1024 * 1024  # 4MB per segment
    num_segments = max(1, (expected_size + segment_size - 1) // segment_size)

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, headers={"Range": "bytes=0-4194303"})
        if resp.status_code == 206:
            part_0 = Path(download_dir) / f"{filename}.part.0"
            part_0.write_bytes(resp.content)

    # Step 2: 创建状态文件，标记第 0 分片已完成
    state_path = Path(download_dir) / f"{filename}.state.json"
    state = {
        "url": url,
        "filename": filename,
        "content_length": expected_size,
        "etag": "",
        "num_segments": num_segments,
        "completed_segments": [0],
        "updated_at": time.time(),
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")

    print(f"\n[断点续传] 已模拟部分下载: 分片 0/{num_segments} 完成")

    # Step 3: 重新下载，应从断点续传
    result = await tool.execute(
        {
            "url": url,
            "save_path": download_dir,
            "filename": filename,
            "max_connections": 4,
            "timeout": 60,
            "skip_ssrf_check": True,
        }
    )

    print(f"[断点续传] 结果: success={result.success}")

    assert result.success, f"续传下载失败: {result.error}"
    data = result.output
    assert data["size"] == expected_size, f"大小不匹配: {data['size']} vs {expected_size}"

    # 验证最终文件 SHA256
    file_path = Path(data["path"])
    actual_hash = _sha256_file(file_path)
    assert actual_hash == expected_hash, f"续传后 SHA256 不匹配: {actual_hash} != {expected_hash}"

    print(f"  文件: {file_path.name}")
    print(f"  大小: {data['size']} 字节")
    print(f"  耗时: {data['duration']}s")
    print(f"  SHA256: {actual_hash[:16]}...")


# ─── 4. 超时处理测试 ────────────────────────────────


@pytest.mark.asyncio
async def test_timeout_handling(tool, download_dir):
    """测试超时场景处理（使用不可达地址）"""
    result = await tool.execute(
        {
            "url": "https://10.255.255.1/file.txt",  # 不可达 IP，必定超时
            "save_path": download_dir,
            "timeout": 2,
            "max_retries": 1,
        }
    )

    print(f"\n[超时处理] 结果: success={result.success}, error={result.error}")

    assert result.success is False, "超时场景应返回失败"
    assert result.error, "应包含错误信息"

    print(f"  错误信息: {result.error}")


@pytest.mark.asyncio
async def test_invalid_url(tool, download_dir):
    """测试无效 URL 处理"""
    result = await tool.execute(
        {
            "url": "https://this-domain-does-not-exist-abcxyz.com/file.txt",
            "save_path": download_dir,
            "timeout": 10,
            "max_retries": 1,
        }
    )

    print(f"\n[无效URL] 结果: success={result.success}")

    assert result.success is False
    print(f"  错误信息: {result.error}")


@pytest.mark.asyncio
async def test_missing_params(tool, download_dir):
    """测试缺少必填参数"""
    result = await tool.execute({"save_path": download_dir})
    assert result.success is False
    assert "url" in result.error

    result = await tool.execute({"url": "https://example.com/file"})
    assert result.success is False
    assert "save_path" in result.error


# ─── 5. 安全校验测试 ────────────────────────────────


def test_sanitize_filename():
    """测试文件名清洗（防路径穿越）"""
    assert _sanitize_filename("normal.txt") == "normal.txt"
    assert _sanitize_filename("../../../etc/passwd") == "passwd"
    assert _sanitize_filename("/absolute/path/file.zip") == "file.zip"
    assert _sanitize_filename("file with spaces.pdf") == "file with spaces.pdf"
    assert _sanitize_filename("") == "download"
    assert _sanitize_filename("...") == "download"
    assert _sanitize_filename("中文文件名.txt") == "中文文件名.txt"


def test_validate_url_protocol():
    """测试协议白名单"""
    valid, _ = _validate_url("https://example.com/file")
    assert valid is True

    valid, msg = _validate_url("ftp://example.com/file")
    assert valid is False
    assert "协议" in msg

    valid, _ = _validate_url("file:///etc/passwd")
    assert valid is False


def test_validate_url_domain_whitelist():
    """测试域名白名单"""
    valid, _ = _validate_url("https://github.com/file", allow_domains=["github.com"])
    assert valid is True

    valid, msg = _validate_url("https://evil.com/file", allow_domains=["github.com"])
    assert valid is False
    assert "白名单" in msg


def test_is_private_ip():
    """测试内网 IP 检测（SSRF 防护）"""
    assert _is_private_ip("127.0.0.1") is True
    assert _is_private_ip("10.0.0.1") is True
    assert _is_private_ip("172.16.0.1") is True
    assert _is_private_ip("192.168.1.1") is True
    assert _is_private_ip("169.254.0.1") is True
    assert _is_private_ip("8.8.8.8") is False
    assert _is_private_ip("1.1.1.1") is False


@pytest.mark.asyncio
async def test_file_size_limit(tool, download_dir, test_server):
    """测试文件大小上限"""
    url = f"{test_server['base_url']}/small.bin"

    result = await tool.execute(
        {
            "url": url,
            "save_path": download_dir,
            "max_size": 1,  # 1 字节上限
            "skip_ssrf_check": True,
        }
    )

    print(f"\n[大小限制] 结果: success={result.success}, error={result.error}")

    assert result.success is False, "超出大小限制应返回失败"
    assert "大小" in result.error or "上限" in result.error, f"错误信息应包含大小限制描述，实际: {result.error}"


@pytest.mark.asyncio
async def test_specified_filename(tool, download_dir, test_server):
    """测试指定文件名"""
    url = f"{test_server['base_url']}/small.bin"

    result = await tool.execute(
        {
            "url": url,
            "save_path": download_dir,
            "filename": "custom_name.bin",
            "timeout": 30,
        }
    )

    assert result.success
    assert Path(result.output["path"]).name == "custom_name.bin"


@pytest.mark.asyncio
async def test_ssrf_protection(tool, download_dir):
    """测试 SSRF 防护（拒绝内网地址）"""
    result = await tool.execute(
        {
            "url": "http://127.0.0.1:6800/secret",
            "save_path": download_dir,
        }
    )

    assert result.success is False
    assert "内网" in result.error or "SSRF" in result.error or "loopback" in result.error.lower(), (
        f"应拦截内网请求，实际: {result.error}"
    )


# ─── 6. 外部网络真实下载测试（可选）───────────────────


@pytest.mark.asyncio
@pytest.mark.skip(reason="需要外部网络，手动运行: pytest -m network")
async def test_download_external_small_file(tool, download_dir):
    """从 GitHub 下载小文件（外部网络测试）"""
    result = await tool.execute(
        {
            "url": "https://raw.githubusercontent.com/python/cpython/main/README.rst",
            "save_path": download_dir,
            "timeout": 60,
            "max_retries": 3,
        }
    )

    print(f"\n[外部小文件下载] 结果: {result}")

    assert result.success, f"下载失败: {result.error}"
    data = result.output
    assert data["size"] > 0

    file_path = Path(data["path"])
    assert file_path.exists()
    content = file_path.read_text(encoding="utf-8", errors="replace")
    assert len(content) > 100

    print(f"  文件: {file_path.name}")
    print(f"  大小: {data['size']} 字节")
    print(f"  耗时: {data['duration']}s")
    print(f"  速度: {data['avg_speed']}")


if __name__ == "__main__":
    pass
